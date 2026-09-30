#!/usr/bin/env python3
"""
Página de gestión de tickets (solo para este Mac).

Escucha en 127.0.0.1:5175 (ADMIN_PORT en .env). El túnel de Cloudflare solo
publica el puerto de la web (5174), así que esta página NO es accesible desde
internet. Además rechaza peticiones con otro Host (DNS rebinding) y exige una
cabecera propia en los cambios (CSRF).

Lee y modifica la base de datos con las mismas funciones del servidor MCP
(mcp_tickets.py). server.py la arranca automáticamente; también se puede usar sola:
    python3 gestion.py
"""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

import mcp_tickets as mt

ROOT = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(ROOT, 'gestion', 'index.html')
ASSETS = {'/favicon.ico': ('img/favicon.ico', 'image/x-icon'), '/logo.png': ('img/faena-symbol.png', 'image/png')}


def make_handler(port):
    allowed_hosts = {f'localhost:{port}', f'127.0.0.1:{port}'}

    class AdminHandler(BaseHTTPRequestHandler):
        server_version = 'FaenaGestion'
        sys_version = ''

        def log_message(self, fmt, *args):
            pass

        def send(self, status, body, ctype='application/json; charset=utf-8'):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.end_headers()
            self.wfile.write(data)

        def fail(self, status, message):
            self.send(status, {'error': {'message': message}})

        def local_only(self):
            # solo este Mac: Host local y sin cabeceras de Cloudflare
            if self.headers.get('Host') not in allowed_hosts or self.headers.get('Cf-Ray') or self.headers.get('Cf-Connecting-Ip'):
                self.fail(403, 'Acceso no permitido')
                return False
            return True

        def do_GET(self):
            if not self.local_only():
                return
            url = urlsplit(self.path)
            path = unquote(url.path)
            if path in ('/', '/index.html'):
                with open(PAGE, 'rb') as f:
                    return self.send(200, f.read(), 'text/html; charset=utf-8')
            if path in ASSETS:
                rel, ctype = ASSETS[path]
                with open(os.path.join(ROOT, rel), 'rb') as f:
                    return self.send(200, f.read(), ctype)
            db = mt.connect()
            try:
                if path == '/api/tickets':
                    q = {k: v[0] for k, v in parse_qs(url.query).items()}
                    q.setdefault('limite', '200')
                    data = mt.tool_listar_tickets(db, q)
                    data['conteo'] = {r[0]: r[1] for r in db.execute('SELECT estado, COUNT(*) FROM tickets GROUP BY estado')}
                    data['p1_abiertos'] = db.execute(
                        "SELECT COUNT(*) FROM tickets WHERE prioridad LIKE 'P1%' AND estado IN ('nuevo', 'en_proceso')").fetchone()[0]
                    return self.send(200, data)
                if path.startswith('/api/tickets/'):
                    return self.send(200, mt.tool_obtener_ticket(db, {'id': path.rsplit('/', 1)[1]}))
                self.fail(404, 'No encontrado')
            except mt.ToolError as e:
                self.fail(404, str(e))
            finally:
                db.close()

        def do_POST(self):
            if not self.local_only():
                return
            if self.headers.get('X-Gestion') != '1':  # impide peticiones de otros sitios (CSRF)
                return self.fail(403, 'Acceso no permitido')
            path = unquote(urlsplit(self.path).path)
            if not path.startswith('/api/tickets/'):
                return self.fail(404, 'No encontrado')
            try:
                args = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)) or b'{}')
                assert isinstance(args, dict)
            except (ValueError, AssertionError):
                return self.fail(400, 'Petición no válida')
            db = mt.connect()
            try:
                args['id'] = path.rsplit('/', 1)[1]
                self.send(200, mt.tool_actualizar_ticket(db, args))
            except mt.ToolError as e:
                self.fail(400, str(e))
            finally:
                db.close()

    return AdminHandler


def start(port, background=True):
    srv = ThreadingHTTPServer(('127.0.0.1', port), make_handler(port))
    srv.daemon_threads = True
    if background:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == '__main__':
    port = int(os.environ.get('ADMIN_PORT', 5175))
    print(f'Gestión de tickets en http://localhost:{port}  (solo accesible desde este Mac)')
    try:
        start(port, background=False).serve_forever()
    except KeyboardInterrupt:
        pass
