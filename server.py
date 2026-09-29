#!/usr/bin/env python3
"""
Servidor de la web del asistente + puente seguro hacia oMLX.

- Sirve solo los archivos públicos de la web (lista blanca).
- Reenvía a oMLX únicamente los 4 endpoints que usa el asistente, añadiendo la
  API key en el servidor (nunca llega al navegador).
- Protege el Mac: límite de tamaño, de max_tokens, de generaciones simultáneas
  y de mensajes por minuto por visitante.

Uso:  python3 server.py        (configuración en .env, ver .env.example)
"""
import http.client
import json
import mimetypes
import os
import posixpath
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

ROOT = os.path.dirname(os.path.abspath(__file__))


def load_env(path):
    env = {}
    try:
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return env


ENV = {**load_env(os.path.join(ROOT, '.env')), **os.environ}
PORT = int(ENV.get('PORT', 5174))
OMLX = urlsplit(ENV.get('OMLX_URL', 'http://127.0.0.1:8000'))
API_KEY = ENV.get('OMLX_API_KEY', '')
MAX_TOKENS = int(ENV.get('MAX_TOKENS', 1024))
MAX_CONCURRENT = int(ENV.get('MAX_CONCURRENT', 2))
RATE_PER_MIN = int(ENV.get('RATE_PER_MIN', 20))
MAX_BODY = int(ENV.get('MAX_BODY_MB', 25)) * 1024 * 1024

# Archivos públicos: solo estos se sirven. Todo lo demás (.env, .git, server.py…) da 404.
PUBLIC_FILES = {'index.html', 'config.js', 'contexto.js'}
PUBLIC_DIRS = ('css/', 'assistant/', 'img/')
PROXY_GET = {'/health', '/v1/models', '/v1/models/status'}
PROXY_CHAT = '/v1/chat/completions'

slots = threading.BoundedSemaphore(MAX_CONCURRENT)
hits = defaultdict(deque)
hits_lock = threading.Lock()


def rate_limited(ip):
    now = time.monotonic()
    with hits_lock:
        q = hits[ip]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= RATE_PER_MIN:
            return True
        q.append(now)
        return False


def upstream(method, path, body=None, timeout=600):
    conn = http.client.HTTPConnection(OMLX.hostname, OMLX.port or 80, timeout=timeout)
    headers = {'Content-Type': 'application/json'}
    if API_KEY:
        headers['Authorization'] = f'Bearer {API_KEY}'
    conn.request(method, path, body=body, headers=headers)
    return conn, conn.getresponse()


class Handler(BaseHTTPRequestHandler):
    server_version = 'FaenaAsistente'
    sys_version = ''

    # ---------- utilidades ----------
    def client_ip(self):
        return self.headers.get('CF-Connecting-IP') or self.client_address[0]

    def send_json(self, status, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def error(self, status, message):
        self.send_json(status, {'error': {'message': message}})

    def end_headers(self):
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'same-origin')
        super().end_headers()

    def log_message(self, fmt, *args):
        pass  # solo se registran las peticiones al chat (ver do_POST)

    # ---------- rutas ----------
    def do_GET(self):
        path = urlsplit(self.path).path
        if path in PROXY_GET:
            return self.proxy_get(path)
        return self.serve_static(path)

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        path = urlsplit(self.path).path
        if path != PROXY_CHAT:
            return self.error(404, 'No encontrado')
        ip, t0 = self.client_ip(), time.time()
        status = self.proxy_chat(ip)
        print(f'{time.strftime("%H:%M:%S")}  chat  {ip:<15}  {status}  {time.time() - t0:5.1f}s', flush=True)

    def do_PUT(self):
        self.error(405, 'Método no permitido')

    do_DELETE = do_PATCH = do_PUT

    # ---------- archivos estáticos ----------
    def serve_static(self, path):
        rel = posixpath.normpath(unquote(path)).lstrip('/')
        if rel in ('', '.'):
            rel = 'index.html'
        allowed = rel in PUBLIC_FILES or (rel.startswith(PUBLIC_DIRS) and '..' not in rel.split('/'))
        full = os.path.realpath(os.path.join(ROOT, rel))
        if not allowed or not full.startswith(ROOT + os.sep) or not os.path.isfile(full):
            return self.error(404, 'No encontrado')
        ctype = mimetypes.guess_type(full)[0] or 'application/octet-stream'
        if ctype.startswith('text/') or ctype.endswith('javascript'):
            ctype += '; charset=utf-8'
        with open(full, 'rb') as f:
            data = f.read()
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        # sin caché para html/css/js: los cambios se ven con una recarga normal
        self.send_header('Cache-Control', 'public, max-age=86400' if rel.startswith('img/') else 'no-cache')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(data)

    # ---------- proxy hacia oMLX ----------
    def proxy_get(self, path):
        try:
            conn, res = upstream('GET', path, timeout=15)
            raw = res.read()
            conn.close()
        except OSError:
            return self.error(502, 'El servidor del modelo no está disponible en este momento.')
        if res.status != 200:
            return self.error(res.status, 'El servidor del modelo respondió con un error.')
        data = json.loads(raw or b'{}')
        if path == '/v1/models/status':
            # no exponer rutas locales, tamaños ni configuración interna
            data = {'models': [{'id': m.get('id'), 'loaded': m.get('loaded'), 'model_type': m.get('model_type')}
                               for m in data.get('models', [])]}
        elif path == '/health':
            data = {'status': data.get('status'), 'default_model': data.get('default_model')}
        self.send_json(200, data)

    def proxy_chat(self, ip):
        length = int(self.headers.get('Content-Length') or 0)
        if length <= 0:
            self.error(400, 'Petición vacía.'); return 400
        if length > MAX_BODY:
            self.error(413, 'El mensaje o los adjuntos son demasiado grandes.'); return 413
        if rate_limited(ip):
            self.error(429, 'Has enviado demasiados mensajes seguidos. Espera un minuto e inténtalo de nuevo.'); return 429
        try:
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict) or not isinstance(body.get('messages'), list):
                raise ValueError
        except ValueError:
            self.error(400, 'Petición no válida.'); return 400
        body['max_tokens'] = min(int(body.get('max_tokens') or MAX_TOKENS), MAX_TOKENS)

        if not slots.acquire(blocking=False):
            self.error(503, 'El asistente está ocupado atendiendo otras consultas. Inténtalo en unos segundos.'); return 503
        conn = None
        try:
            try:
                conn, res = upstream('POST', PROXY_CHAT, json.dumps(body).encode())
            except OSError:
                self.error(502, 'El servidor del modelo no está disponible en este momento.'); return 502
            self.send_response(res.status)
            self.send_header('Content-Type', res.getheader('Content-Type', 'application/json'))
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('X-Accel-Buffering', 'no')
            self.send_header('Connection', 'close')
            self.end_headers()
            # reenvío en streaming: cada bloque sale en cuanto llega de oMLX
            while True:
                chunk = res.read1(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
            return res.status
        except (BrokenPipeError, ConnectionResetError):
            return 499  # el visitante cerró la conexión: se corta también la generación
        finally:
            if conn:
                conn.close()
            slots.release()


def main():
    if not API_KEY:
        print('Aviso: OMLX_API_KEY no está definida en .env; se llamará a oMLX sin clave.')
    srv = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    srv.daemon_threads = True
    print(f'Asistente en http://localhost:{PORT}  →  oMLX en {OMLX.geturl()}  '
          f'(máx. {MAX_CONCURRENT} simultáneas, {RATE_PER_MIN} msg/min por visitante, max_tokens {MAX_TOKENS})', flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
