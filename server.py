#!/usr/bin/env python3
"""
Servidor de la web del asistente + puente seguro hacia oMLX.

- Sirve solo los archivos públicos de la web (lista blanca).
- Reenvía a oMLX únicamente los 4 endpoints que usa el asistente, añadiendo la
  API key en el servidor (nunca llega al navegador).
- Protege el Mac: límite de tamaño, de max_tokens, de generaciones simultáneas
  y de mensajes por minuto por visitante.
- Registra las solicitudes validadas por los usuarios (POST /api/tickets) en la
  base de datos de tickets a través de MCP: oMLX ejecuta la herramienta
  tickets__crear_ticket del servidor mcp_tickets.py (ver mcp.json).

Uso:  python3 server.py            (configuración en .env, ver .env.example)
      python3 mcp_tickets.py listar   (lista los tickets registrados)
"""
import base64
import http.client
import json
import mimetypes
import os
import posixpath
import re
import sys
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
ADMIN_PORT = int(ENV.get('ADMIN_PORT', 5175))  # página de gestión de tickets, solo local (0 = desactivada)
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
TICKETS_API = '/api/tickets'
TICKETS_MCP_TOOL = ENV.get('TICKETS_MCP_TOOL', 'tickets__crear_ticket')
ATTACH_MCP_TOOL = ENV.get('ATTACH_MCP_TOOL', 'tickets__adjuntar_archivo')
# evidencias adjuntas a una solicitud
MAX_FILES = 10
MAX_FILE = int(ENV.get('MAX_ADJUNTO_MB', 10)) * 1024 * 1024
MAX_FILES_TOTAL = int(ENV.get('MAX_ADJUNTOS_TOTAL_MB', 25)) * 1024 * 1024
TICKET_MAX_BODY = MAX_FILES_TOTAL * 4 // 3 + 2 * 1024 * 1024   # base64 + datos del ticket
# texto y código: se guardan como text/plain (nunca se ejecutan ni se muestran como HTML)
TEXT_EXTS = {'txt', 'log', 'out', 'err', 'trace', 'csv', 'tsv', 'json', 'xml', 'md', 'markdown', 'yaml', 'yml', 'ini', 'toml',
             'sql', 'conf', 'cfg', 'html', 'htm', 'css', 'js', 'jsx', 'ts', 'tsx', 'py', 'java', 'c', 'h', 'cpp', 'cs', 'go',
             'rs', 'rb', 'php', 'swift', 'kt', 'sh', 'tex', 'rtf'}
TICKETS_PER_MIN = int(ENV.get('TICKETS_PER_MIN', 5))
# campo -> largo máximo; los obligatorios se validan aparte
TICKET_FIELDS = {'tipo': 60, 'titulo': 200, 'categoria': 200, 'descripcion': 4000, 'nombre': 120,
                 'correo': 200, 'telefono': 60, 'prioridad': 60, 'equipo': 120, 'evidencias': 600}
TICKET_REQUIRED = ('titulo', 'descripcion', 'nombre', 'correo')
EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')

slots = threading.BoundedSemaphore(MAX_CONCURRENT)
hits = defaultdict(deque)
hits_lock = threading.Lock()


def sniff_type(data, name):
    """Tipo MIME según el contenido real del archivo (no el que declara el navegador); None si no se admite."""
    ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if data[:6] in (b'GIF87a', b'GIF89a'):
        return 'image/gif'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    if data.startswith(b'%PDF-'):
        return 'application/pdf'
    if data.startswith(b'PK\x03\x04') and ext == 'docx':
        return 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    if data.startswith(b'PK\x03\x04') and ext == 'xlsx':
        return 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    if data.startswith(b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1') and ext == 'xls':
        return 'application/vnd.ms-excel'
    if (ext in TEXT_EXTS or re.fullmatch(r'log\.\d+', ext) or name.lower().endswith(tuple(f'.log.{i}' for i in range(10)))) \
            and b'\x00' not in data[:8192]:
        return 'text/plain'
    return None


def parse_files(raw):
    """Valida las evidencias enviadas. Devuelve (lista, None) o (None, mensaje de error)."""
    if raw is None:
        return [], None
    if not isinstance(raw, list) or len(raw) > MAX_FILES:
        return None, f'Puedes adjuntar como máximo {MAX_FILES} archivos por solicitud.'
    files, total = [], 0
    for f in raw:
        if not isinstance(f, dict):
            return None, 'Adjunto no válido.'
        name = os.path.basename(str(f.get('nombre') or 'archivo').replace('\\', '/'))[:200] or 'archivo'
        try:
            data = base64.b64decode(str(f.get('contenido_base64') or ''), validate=True)
        except ValueError:
            return None, f'El archivo «{name}» está dañado.'
        if not data:
            continue
        if len(data) > MAX_FILE:
            return None, f'«{name}» supera el máximo de {MAX_FILE // 1048576} MB por archivo.'
        total += len(data)
        if total > MAX_FILES_TOTAL:
            return None, f'Los adjuntos superan el máximo de {MAX_FILES_TOTAL // 1048576} MB por solicitud.'
        mime = sniff_type(data, name)
        if not mime:
            return None, f'«{name}» no es un tipo de archivo admitido como evidencia.'
        files.append({'nombre': name, 'tipo': mime, 'contenido_base64': base64.b64encode(data).decode()})
    return files, None


def mcp_call(tool, args, timeout=60):
    """Ejecuta una herramienta MCP a través de oMLX y devuelve su resultado (JSON). Lanza excepción si falla."""
    conn, res = upstream('POST', '/v1/mcp/execute', json.dumps({'tool_name': tool, 'arguments': args}).encode(), timeout=timeout)
    out = json.loads(res.read() or b'{}')
    conn.close()
    if res.status != 200 or out.get('is_error'):
        raise RuntimeError(out.get('error_message') or out.get('content') or out.get('detail') or res.status)
    return json.loads(out['content']) if isinstance(out.get('content'), str) else out.get('content')


def rate_limited(key, limit=RATE_PER_MIN):
    now = time.monotonic()
    with hits_lock:
        q = hits[key]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= limit:
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
        if path == TICKETS_API:
            return self.create_ticket()
        if path != PROXY_CHAT:
            return self.error(404, 'No encontrado')
        ip, t0 = self.client_ip(), time.time()
        status = self.proxy_chat(ip)
        print(f'{time.strftime("%H:%M:%S")}  chat  {ip:<15}  {status}  {time.time() - t0:5.1f}s', flush=True)

    def do_PUT(self):
        self.error(405, 'Método no permitido')

    do_DELETE = do_PATCH = do_PUT

    # ---------- registro de solicitudes ----------
    def create_ticket(self):
        ip = self.client_ip()
        length = int(self.headers.get('Content-Length') or 0)
        if length <= 0:
            return self.error(400, 'Solicitud no válida.')
        if length > TICKET_MAX_BODY:
            return self.error(413, f'Los adjuntos superan el máximo de {MAX_FILES_TOTAL // 1048576} MB por solicitud.')
        if rate_limited('ticket:' + ip, TICKETS_PER_MIN):
            return self.error(429, 'Has enviado demasiadas solicitudes seguidas. Espera un minuto.')
        try:
            data = json.loads(self.rfile.read(length))
            assert isinstance(data, dict)
        except (ValueError, AssertionError):
            return self.error(400, 'Solicitud no válida.')
        ticket = {k: str(data.get(k) or '').strip()[:n] for k, n in TICKET_FIELDS.items()}
        missing = [k for k in TICKET_REQUIRED if not ticket[k]]
        if missing:
            return self.error(400, 'Faltan datos obligatorios: ' + ', '.join(missing) + '.')
        if not EMAIL_RE.match(ticket['correo']):
            return self.error(400, 'El correo no parece válido. Corrígelo y vuelve a enviar.')
        files, problem = parse_files(data.get('archivos'))
        if problem:
            return self.error(400, problem)
        conv = data.get('conversacion') if isinstance(data.get('conversacion'), list) else []
        args = {
            **ticket,
            'conversacion': [
                {'rol': str(m.get('role', ''))[:20], 'texto': str(m.get('content', ''))[:4000],
                 'adjuntos': [str(a)[:200] for a in (m.get('adjuntos') or [])][:10]}
                for m in conv[-60:] if isinstance(m, dict)
            ],
            'ip': ip,
        }
        # registro en la base de datos a través de MCP (oMLX ejecuta la herramienta del servidor «tickets»)
        try:
            result = mcp_call(TICKETS_MCP_TOOL, args, timeout=30)
            tid, fecha = result['id'], result['fecha']
        except Exception as e:  # noqa: BLE001 — cualquier fallo del registro se informa igual al usuario
            print(f'{time.strftime("%H:%M:%S")}  TICKET ERROR  {e}', flush=True)
            return self.error(503, 'No se pudo registrar la solicitud en este momento. Inténtalo en unos minutos.')
        saved, failed = 0, []
        for f in files:  # evidencias: una llamada MCP por archivo
            try:
                mcp_call(ATTACH_MCP_TOOL, {'ticket_id': tid, **f}, timeout=120)
                saved += 1
            except Exception as e:  # noqa: BLE001
                failed.append(f['nombre'])
                print(f'{time.strftime("%H:%M:%S")}  ADJUNTO ERROR  {tid} {f["nombre"]}: {e}', flush=True)
        print(f'{time.strftime("%H:%M:%S")}  TICKET {tid}  {ticket["tipo"] or "-"} · {ticket["titulo"]} · {ticket["nombre"]} <{ticket["correo"]}>'
              + (f'  📎 {saved}' if saved else ''), flush=True)
        self.send_json(201, {'id': tid, 'fecha': fecha, 'adjuntos': saved, 'adjuntos_fallidos': failed})

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
        # los visitantes nunca pueden usar herramientas (p. ej. las MCP de tickets que oMLX añade a los chats)
        body.pop('tools', None)
        body['tool_choice'] = 'none'

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
    if len(sys.argv) > 1 and sys.argv[1] == 'tickets':
        return print('Los tickets ahora están en la base de datos. Usa:  python3 mcp_tickets.py listar')
    if not API_KEY:
        print('Aviso: OMLX_API_KEY no está definida en .env; se llamará a oMLX sin clave.')
    srv = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    srv.daemon_threads = True
    print(f'Asistente en http://localhost:{PORT}  →  oMLX en {OMLX.geturl()}  '
          f'(máx. {MAX_CONCURRENT} simultáneas, {RATE_PER_MIN} msg/min por visitante, max_tokens {MAX_TOKENS})', flush=True)
    if ADMIN_PORT:
        import gestion  # página de gestión de tickets: puerto aparte, nunca publicado por el túnel
        try:
            gestion.start(ADMIN_PORT)
            print(f'Gestión de tickets en http://localhost:{ADMIN_PORT}  (solo desde este Mac)', flush=True)
        except OSError:
            print(f'Aviso: el puerto {ADMIN_PORT} está ocupado; la página de gestión no se inició.', flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
