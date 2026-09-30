#!/usr/bin/env python3
"""
Servidor MCP «tickets»: registro de solicitudes de la Mesa de Ayuda en SQLite.

Habla el protocolo MCP por stdio (JSON-RPC, un mensaje por línea) usando solo la
biblioteca estándar, así que funciona con el Python 3.9 de macOS sin instalar nada.

Herramientas:  crear_ticket · listar_tickets · obtener_ticket · actualizar_ticket
               adjuntar_archivo · obtener_adjunto  (evidencias guardadas como BLOB)
Base de datos: tickets/tickets.db  (carpeta privada, ignorada por git)

Uso:
  python3 mcp_tickets.py           servidor MCP (lo arranca oMLX, Claude Desktop, etc.)
  python3 mcp_tickets.py listar    muestra los tickets en la terminal
"""
import base64
import hashlib
import json
import os
import re
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get('TICKETS_DB', os.path.join(ROOT, 'tickets', 'tickets.db'))
# registro anterior de server.py; solo se importa si está junto a la base de datos
LEGACY_JSONL = os.path.join(os.path.dirname(DB_PATH), 'tickets.jsonl')
SERVER_INFO = {'name': 'tickets', 'version': '1.0.0'}
PROTOCOL_VERSION = '2025-06-18'

ESTADOS = ('nuevo', 'en_proceso', 'resuelto', 'cerrado')
CAMPOS = {'tipo': 60, 'titulo': 200, 'categoria': 200, 'descripcion': 4000, 'nombre': 120,
          'correo': 200, 'telefono': 60, 'prioridad': 60, 'equipo': 120, 'evidencias': 600}
OBLIGATORIOS = ('titulo', 'descripcion', 'nombre', 'correo')
EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
MAX_ADJUNTO = int(os.environ.get('MAX_ADJUNTO_MB', 10)) * 1024 * 1024
MAX_ADJUNTOS_POR_TICKET = 10


class ToolError(Exception):
    """Error de validación que se devuelve al cliente como resultado con isError."""


# ---------------------------------------------------------------- base de datos
def connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)  # autocommit; transacciones explícitas
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.executescript("""
        CREATE TABLE IF NOT EXISTS tickets (
            num          INTEGER PRIMARY KEY AUTOINCREMENT,
            id           TEXT UNIQUE NOT NULL,
            fecha        TEXT NOT NULL,
            actualizado  TEXT NOT NULL,
            estado       TEXT NOT NULL DEFAULT 'nuevo',
            tipo TEXT, titulo TEXT NOT NULL, categoria TEXT, descripcion TEXT NOT NULL,
            nombre TEXT NOT NULL, correo TEXT NOT NULL, telefono TEXT,
            prioridad TEXT, equipo TEXT, evidencias TEXT,
            conversacion TEXT, ip TEXT
        );
        CREATE TABLE IF NOT EXISTS comentarios (
            num        INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id  TEXT NOT NULL REFERENCES tickets(id),
            fecha      TEXT NOT NULL,
            texto      TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS adjuntos (
            num        INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id  TEXT NOT NULL REFERENCES tickets(id),
            fecha      TEXT NOT NULL,
            nombre     TEXT NOT NULL,
            tipo       TEXT NOT NULL,
            tamano     INTEGER NOT NULL,
            sha256     TEXT NOT NULL,
            contenido  BLOB NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_tickets_estado ON tickets(estado);
        CREATE INDEX IF NOT EXISTS idx_adjuntos_ticket ON adjuntos(ticket_id);
    """)
    import_legacy(db)
    return db


def now():
    return time.strftime('%Y-%m-%dT%H:%M:%S%z')


def insert_ticket(db, data, fecha=None):
    """Inserta un ticket y le asigna el siguiente número TCK-NNNN de forma atómica."""
    t = {k: str(data.get(k) or '').strip()[:n] for k, n in CAMPOS.items()}
    faltan = [k for k in OBLIGATORIOS if not t[k]]
    if faltan:
        raise ToolError('Faltan datos obligatorios: ' + ', '.join(faltan))
    if not EMAIL_RE.match(t['correo']):
        raise ToolError('El correo no es válido: ' + t['correo'])
    conv = data.get('conversacion')
    conv = json.dumps(conv, ensure_ascii=False) if isinstance(conv, list) else None
    fecha = fecha or now()
    db.execute('BEGIN IMMEDIATE')
    try:
        cur = db.execute(
            'INSERT INTO tickets (id, fecha, actualizado, estado, tipo, titulo, categoria, descripcion, nombre, correo,'
            ' telefono, prioridad, equipo, evidencias, conversacion, ip)'
            ' VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            ('pendiente-' + str(time.time_ns()), fecha, fecha, 'nuevo', t['tipo'], t['titulo'], t['categoria'],
             t['descripcion'], t['nombre'], t['correo'], t['telefono'], t['prioridad'], t['equipo'],
             t['evidencias'], conv, str(data.get('ip') or '')[:64]))
        tid = f'TCK-{cur.lastrowid:04d}'
        db.execute('UPDATE tickets SET id = ? WHERE num = ?', (tid, cur.lastrowid))
        db.execute('COMMIT')
    except Exception:
        db.execute('ROLLBACK')
        raise
    return tid, fecha


def import_legacy(db):
    """Importa una sola vez los tickets del registro anterior (tickets.jsonl)."""
    if not os.path.exists(LEGACY_JSONL):
        return
    n = 0
    with open(LEGACY_JSONL, encoding='utf-8') as f:
        for line in f:
            if line.strip():
                old = json.loads(line)
                insert_ticket(db, old, fecha=old.get('fecha'))
                n += 1
    os.replace(LEGACY_JSONL, LEGACY_JSONL + '.importado')
    print(f'Importados {n} ticket(s) de tickets.jsonl', file=sys.stderr)


def resumen(row):
    r = {k: row[k] for k in ('id', 'fecha', 'estado', 'prioridad', 'tipo', 'titulo', 'equipo', 'nombre', 'correo')}
    r['adjuntos'] = row['n_adjuntos'] if 'n_adjuntos' in row.keys() else 0
    return r


def lista_adjuntos(db, ticket_id):
    return [dict(r) for r in db.execute(
        'SELECT num AS id, nombre, tipo, tamano, sha256, fecha FROM adjuntos WHERE ticket_id = ? ORDER BY num', (ticket_id,))]


def leer_adjunto(db, adjunto_id):
    """Devuelve (nombre, tipo, bytes) de un adjunto, o None si no existe."""
    row = db.execute('SELECT nombre, tipo, contenido FROM adjuntos WHERE num = ?', (int(adjunto_id),)).fetchone()
    return (row['nombre'], row['tipo'], bytes(row['contenido'])) if row else None


# ---------------------------------------------------------------- herramientas
def tool_crear_ticket(db, args):
    tid, fecha = insert_ticket(db, args)
    print(f'{fecha}  creado {tid}: {args.get("titulo")}', file=sys.stderr)
    return {'id': tid, 'fecha': fecha, 'estado': 'nuevo'}


def tool_listar_tickets(db, args):
    where, params = [], []
    for campo in ('estado', 'prioridad', 'equipo'):
        if args.get(campo):
            where.append(f'{campo} = ?'); params.append(args[campo])
    if args.get('texto'):
        where.append('(titulo LIKE ? OR descripcion LIKE ? OR nombre LIKE ? OR correo LIKE ?)')
        params += ['%' + args['texto'] + '%'] * 4
    limite = max(1, min(int(args.get('limite') or 20), 200))
    sql = ('SELECT t.*, (SELECT COUNT(*) FROM adjuntos a WHERE a.ticket_id = t.id) AS n_adjuntos FROM tickets t'
           + (' WHERE ' + ' AND '.join(where) if where else '') + ' ORDER BY num DESC LIMIT ?')
    rows = db.execute(sql, params + [limite]).fetchall()
    total = db.execute('SELECT COUNT(*) FROM tickets' + (' WHERE ' + ' AND '.join(where) if where else ''), params).fetchone()[0]
    return {'total': total, 'tickets': [resumen(r) for r in rows]}


def tool_obtener_ticket(db, args):
    row = db.execute('SELECT * FROM tickets WHERE id = ?', (str(args.get('id', '')).upper(),)).fetchone()
    if not row:
        raise ToolError(f'No existe el ticket {args.get("id")}')
    t = dict(row)
    t.pop('num', None)
    t['conversacion'] = json.loads(t['conversacion']) if t['conversacion'] else []
    t['comentarios'] = [dict(fecha=c['fecha'], texto=c['texto']) for c in
                        db.execute('SELECT fecha, texto FROM comentarios WHERE ticket_id = ? ORDER BY num', (t['id'],))]
    t['adjuntos'] = lista_adjuntos(db, t['id'])
    return t


def tool_adjuntar_archivo(db, args):
    tid = str(args.get('ticket_id', '')).upper()
    if not db.execute('SELECT 1 FROM tickets WHERE id = ?', (tid,)).fetchone():
        raise ToolError(f'No existe el ticket {tid}')
    nombre = os.path.basename(str(args.get('nombre') or '').replace('\\', '/')).strip()[:200] or 'archivo'
    tipo = str(args.get('tipo') or 'application/octet-stream')[:100]
    try:
        data = base64.b64decode(str(args.get('contenido_base64') or ''), validate=True)
    except ValueError:
        raise ToolError('contenido_base64 no es base64 válido')
    if not data:
        raise ToolError('El archivo está vacío')
    if len(data) > MAX_ADJUNTO:
        raise ToolError(f'El archivo supera el máximo de {MAX_ADJUNTO // 1048576} MB')
    if db.execute('SELECT COUNT(*) FROM adjuntos WHERE ticket_id = ?', (tid,)).fetchone()[0] >= MAX_ADJUNTOS_POR_TICKET:
        raise ToolError(f'El ticket ya tiene el máximo de {MAX_ADJUNTOS_POR_TICKET} adjuntos')
    sha = hashlib.sha256(data).hexdigest()
    cur = db.execute('INSERT INTO adjuntos (ticket_id, fecha, nombre, tipo, tamano, sha256, contenido) VALUES (?, ?, ?, ?, ?, ?, ?)',
                     (tid, now(), nombre, tipo, len(data), sha, sqlite3.Binary(data)))
    print(f'{now()}  adjunto {cur.lastrowid} → {tid}: {nombre} ({len(data)} bytes)', file=sys.stderr)
    return {'adjunto_id': cur.lastrowid, 'ticket_id': tid, 'nombre': nombre, 'tamano': len(data), 'sha256': sha}


def tool_obtener_adjunto(db, args):
    found = leer_adjunto(db, args.get('adjunto_id') or 0)
    if not found:
        raise ToolError(f'No existe el adjunto {args.get("adjunto_id")}')
    nombre, tipo, data = found
    return {'adjunto_id': int(args['adjunto_id']), 'nombre': nombre, 'tipo': tipo, 'tamano': len(data),
            'contenido_base64': base64.b64encode(data).decode()}


def tool_actualizar_ticket(db, args):
    tid = str(args.get('id', '')).upper()
    if not db.execute('SELECT 1 FROM tickets WHERE id = ?', (tid,)).fetchone():
        raise ToolError(f'No existe el ticket {tid}')
    estado, comentario = args.get('estado'), str(args.get('comentario') or '').strip()
    if not estado and not comentario:
        raise ToolError('Indica un nuevo estado y/o un comentario')
    if estado and estado not in ESTADOS:
        raise ToolError('Estado no válido. Usa: ' + ', '.join(ESTADOS))
    fecha = now()
    if estado:
        db.execute('UPDATE tickets SET estado = ?, actualizado = ? WHERE id = ?', (estado, fecha, tid))
        comentario = f'[estado → {estado}] {comentario}'.strip()
    else:
        db.execute('UPDATE tickets SET actualizado = ? WHERE id = ?', (fecha, tid))
    db.execute('INSERT INTO comentarios (ticket_id, fecha, texto) VALUES (?, ?, ?)', (tid, fecha, comentario[:2000]))
    return tool_obtener_ticket(db, {'id': tid}) | {'conversacion': '(omitida)'}


str_prop = lambda d: {'type': 'string', 'description': d}
TOOLS = {
    'crear_ticket': (tool_crear_ticket, {
        'description': 'Registra una solicitud o incidente de la Mesa de Ayuda y devuelve su número (TCK-NNNN).',
        'inputSchema': {'type': 'object', 'required': list(OBLIGATORIOS), 'properties': {
            'tipo': str_prop('Incidente o Solicitud de servicio'),
            'titulo': str_prop('Título breve'),
            'categoria': str_prop('Categoría, p. ej. "Licencias > Power BI"'),
            'descripcion': str_prop('Descripción completa'),
            'nombre': str_prop('Nombre del usuario'),
            'correo': str_prop('Correo del usuario'),
            'telefono': str_prop('Teléfono (opcional)'),
            'prioridad': str_prop('P1, P2, P3 o P4'),
            'equipo': str_prop('Equipo resolutor sugerido'),
            'evidencias': str_prop('Archivos adjuntos o "ninguna"'),
            'conversacion': {'type': 'array', 'description': 'Mensajes de la conversación', 'items': {'type': 'object'}},
            'ip': str_prop('IP de origen (uso interno)'),
        }},
    }),
    'listar_tickets': (tool_listar_tickets, {
        'description': 'Lista tickets (los más recientes primero), con filtros opcionales.',
        'inputSchema': {'type': 'object', 'properties': {
            'estado': {'type': 'string', 'enum': list(ESTADOS)},
            'prioridad': str_prop('P1, P2, P3 o P4'),
            'equipo': str_prop('Equipo resolutor exacto'),
            'texto': str_prop('Busca en título, descripción, nombre o correo'),
            'limite': {'type': 'integer', 'description': 'Máximo de resultados (por defecto 20)'},
        }},
    }),
    'obtener_ticket': (tool_obtener_ticket, {
        'description': 'Devuelve un ticket completo, con su conversación y comentarios.',
        'inputSchema': {'type': 'object', 'required': ['id'], 'properties': {'id': str_prop('Número, p. ej. TCK-0001')}},
    }),
    'adjuntar_archivo': (tool_adjuntar_archivo, {
        'description': 'Guarda un archivo de evidencia (pantallazo, log, documento) en un ticket existente.',
        'inputSchema': {'type': 'object', 'required': ['ticket_id', 'nombre', 'contenido_base64'], 'properties': {
            'ticket_id': str_prop('Número del ticket, p. ej. TCK-0001'),
            'nombre': str_prop('Nombre del archivo'),
            'tipo': str_prop('Tipo MIME, p. ej. image/png'),
            'contenido_base64': str_prop('Contenido del archivo en base64'),
        }},
    }),
    'obtener_adjunto': (tool_obtener_adjunto, {
        'description': 'Devuelve un archivo adjunto de un ticket (contenido en base64). Los ids aparecen en obtener_ticket.',
        'inputSchema': {'type': 'object', 'required': ['adjunto_id'], 'properties': {
            'adjunto_id': {'type': 'integer', 'description': 'Id del adjunto'},
        }},
    }),
    'actualizar_ticket': (tool_actualizar_ticket, {
        'description': 'Cambia el estado de un ticket (nuevo, en_proceso, resuelto, cerrado) y/o agrega un comentario.',
        'inputSchema': {'type': 'object', 'required': ['id'], 'properties': {
            'id': str_prop('Número, p. ej. TCK-0001'),
            'estado': {'type': 'string', 'enum': list(ESTADOS)},
            'comentario': str_prop('Nota para el historial del ticket'),
        }},
    }),
}


# ---------------------------------------------------------------- protocolo MCP (JSON-RPC por stdio)
def handle(db, msg):
    method, params, mid = msg.get('method'), msg.get('params') or {}, msg.get('id')
    if method == 'initialize':
        return {'protocolVersion': params.get('protocolVersion') or PROTOCOL_VERSION,
                'capabilities': {'tools': {'listChanged': False}}, 'serverInfo': SERVER_INFO}
    if method == 'ping':
        return {}
    if method == 'tools/list':
        return {'tools': [{'name': name, **spec} for name, (_, spec) in TOOLS.items()]}
    if method == 'tools/call':
        name = params.get('name')
        if name not in TOOLS:
            raise LookupError(f'Herramienta desconocida: {name}')
        try:
            result = TOOLS[name][0](db, params.get('arguments') or {})
            return {'content': [{'type': 'text', 'text': json.dumps(result, ensure_ascii=False)}], 'isError': False}
        except ToolError as e:
            return {'content': [{'type': 'text', 'text': str(e)}], 'isError': True}
    raise NotImplementedError(method)


def serve():
    db = connect()
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'JSON no válido'}}
        else:
            if 'id' not in msg:  # notificación (p. ej. notifications/initialized): sin respuesta
                continue
            try:
                reply = {'jsonrpc': '2.0', 'id': msg['id'], 'result': handle(db, msg)}
            except NotImplementedError as e:
                reply = {'jsonrpc': '2.0', 'id': msg['id'], 'error': {'code': -32601, 'message': f'Método no soportado: {e}'}}
            except Exception as e:  # noqa: BLE001 — cualquier fallo se informa al cliente
                reply = {'jsonrpc': '2.0', 'id': msg['id'], 'error': {'code': -32603, 'message': str(e)}}
        sys.stdout.write(json.dumps(reply, ensure_ascii=False) + '\n')
        sys.stdout.flush()


def listar():
    db = connect()
    rows = db.execute('SELECT t.*, (SELECT COUNT(*) FROM adjuntos a WHERE a.ticket_id = t.id) AS n FROM tickets t ORDER BY num').fetchall()
    if not rows:
        print('Aún no hay tickets registrados.')
        return
    for t in rows:
        print(f"{t['id']}  {t['fecha'][:16].replace('T', ' ')}  {t['estado']:<10} {t['prioridad'] or '-':<4} {t['tipo'] or '-':<22} {t['titulo']}")
        print(f"          {t['nombre']} <{t['correo']}>  tel: {t['telefono'] or '-'}  → {t['equipo'] or '-'}" + (f"  📎 {t['n']}" if t['n'] else ''))
    print(f'\n{len(rows)} ticket(s) en {os.path.relpath(DB_PATH)}')


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'listar':
        listar()
    else:
        serve()
