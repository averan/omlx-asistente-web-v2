# Faena · Asistente local con oMLX

Página web de demostración con un asistente de chat flotante (esquina inferior derecha) que responde usando el modelo que tengas cargado en **oMLX**.

HTML, CSS y JavaScript puros: sin dependencias ni paso de compilación.

## Estructura

```
index.html              Página demo
css/site.css            Estilos de la página
assistant/assistant.css Estilos del widget (todo bajo .oa-root)
assistant/assistant.js  Lógica del widget (se monta solo en <body>)
config.js               Configuración de la web: nombre, saludo, modelo, límites…
server.py               Servidor de la web + puente seguro hacia oMLX (guarda la API key)
publicar.sh             Publica la web en internet con un túnel de Cloudflare
mcp_tickets.py          Servidor MCP «tickets»: registro de solicitudes en SQLite
gestion.py, gestion/    Página de gestión de tickets (solo local, puerto 5175)
mcp.example.json        Plantilla para conectar ese servidor MCP a oMLX
.env                    Tu API key y ajustes del servidor (privado, ignorado por git)
contexto.js             Qué sabe el asistente, de qué habla y cómo responde
img/                    Logo de Faena (símbolo, texto) y favicons
```

## Primeros pasos

1. Asegúrate de que oMLX está en marcha en `http://localhost:8000`.
2. Crea tu configuración privada con la API key de oMLX (no se sube a git):

   ```bash
   cp .env.example .env
   ```

   y edita `.env` para poner tu clave en `OMLX_API_KEY`.
3. Arranca el servidor y abre http://localhost:5174:

   ```bash
   python3 server.py
   ```

La API key vive solo en `.env` y la usa `server.py`: nunca llega al navegador. Por eso abrir `index.html` con doble clic ya no funciona; usa siempre `server.py`.

## Publicar en internet

```bash
brew install cloudflared   # solo la primera vez
./publicar.sh
```

El script arranca `server.py`, abre un túnel gratuito de Cloudflare y muestra la URL pública (`https://….trycloudflare.com`). Cualquiera con esa URL puede usar el asistente, que responde con el modelo cargado en **este Mac**. Ctrl+C deja de publicar.

- **Requisitos:** oMLX en marcha y el Mac encendido (el script evita que se duerma mientras publica).
- **La URL cambia** cada vez que ejecutas el script y no tiene garantía de disponibilidad. Para una dirección fija (p. ej. `soporte.faenacs.com`) se usa un túnel con nombre y una cuenta gratuita de Cloudflare; no hace falta cambiar el código.
- **Qué se expone:** solo los archivos de la web (`index.html`, `config.js`, `contexto.js`, `css/`, `assistant/`, `img/`) y 4 endpoints de oMLX (`/v1/chat/completions`, `/v1/models`, `/v1/models/status` resumido y `/health`). Todo lo demás, incluido `.env`, da 404.
- **Protecciones** (ajustables en `.env`): máximo 2 respuestas generándose a la vez (`MAX_CONCURRENT`), 20 mensajes por minuto por visitante (`RATE_PER_MIN`), respuestas de hasta 1024 tokens (`MAX_TOKENS`) y mensajes de hasta 25 MB (`MAX_BODY_MB`).
- **Ten en cuenta:** quien tenga la URL usa la potencia de tu Mac. El prompt de sistema (`contexto.js`) se ejecuta en el navegador, así que alguien con conocimientos técnicos podría modificarlo en su propia sesión.

## Solicitudes a la Mesa de Ayuda (tickets)

El asistente atiende dos tipos de pedidos: **problemas** (incidentes) y **solicitudes de servicio** (licencias, accesos a Jira u otros sistemas, VPN, instalación de software, equipos). Reúne los datos, pide nombre y correo (y teléfono opcional), **redacta él mismo la descripción** y presenta la solicitud para que el usuario la valide.

Debajo de la solicitud aparecen los botones **Enviar solicitud** y **Corregir**. Al enviarla, el usuario ve su número de ticket (`TCK-0001`, `TCK-0002`…).

### Registro en base de datos vía MCP

```
Web ─«Enviar solicitud»→ server.py ─/v1/mcp/execute→ oMLX ─stdio→ mcp_tickets.py → tickets/tickets.db (SQLite)
```

`mcp_tickets.py` es un servidor **MCP** (sin dependencias, Python 3.9+) con 6 herramientas: `crear_ticket`, `listar_tickets`, `obtener_ticket`, `actualizar_ticket` (estados `nuevo`, `en_proceso`, `resuelto`, `cerrado`, con historial de comentarios), `adjuntar_archivo` y `obtener_adjunto`. La base de datos `tickets/` es privada: no se sirve por la web ni se sube a git.

**Evidencias:** los archivos que el usuario adjunta en la conversación (pantallazos, logs, PDF, Word, Excel) se guardan **originales** en la tabla `adjuntos` de la base de datos (con su tamaño y huella SHA-256) al pulsar «Enviar solicitud», una llamada MCP por archivo.

- Límites: 10 archivos y 10 MB por archivo, 25 MB por solicitud (`MAX_ADJUNTO_MB`, `MAX_ADJUNTOS_TOTAL_MB` en `.env`).
- `server.py` detecta el tipo real por su contenido: solo admite imágenes PNG/JPEG/GIF/WebP, PDF, Word, Excel y archivos de texto o log. Rechaza ejecutables, SVG o archivos disfrazados, antes de crear el ticket.
- El widget conserva los originales en memoria hasta el envío. Si el usuario recarga la página antes de enviar, se guarda la imagen reducida o el texto extraído del documento.
- En la página de gestión, cada ticket muestra sus evidencias: las imágenes se ven en miniatura y el resto se descarga (nunca se abren en el navegador).

**Puesta en marcha (una vez):**

1. `cp mcp.example.json mcp.json` y pon la ruta absoluta de `mcp_tickets.py`.
2. En oMLX: panel → Settings → MCP → *Config path* = ruta de tu `mcp.json` (o `mcp.config_path` en `~/.omlx/settings.json`).
3. Reinicia oMLX (`omlx restart`). En `http://localhost:8000/health` debe aparecer `"mcp": {"servers_connected": 1, "tools_available": 4}`.

**Ver y gestionar tickets:**

- **Página de gestión**: abre http://localhost:5175 mientras corre `server.py` o `publicar.sh`. Muestra el resumen por estado, filtros y búsqueda, y el detalle de cada ticket con su conversación; permite cambiar el estado y agregar comentarios al historial. Se actualiza sola cada 15 s.
  Es **solo local**: escucha en un puerto aparte que el túnel no publica, rechaza otros hosts y peticiones de otros sitios. Se puede cambiar el puerto con `ADMIN_PORT` en `.env` (`0` la desactiva) o abrirla sola con `python3 gestion.py`.
- **Terminal**: `python3 mcp_tickets.py listar`, o consultas SQL con `sqlite3 -box tickets/tickets.db "SELECT id, estado, titulo FROM tickets;"`.

Con `expose_tools` activado en oMLX, también puedes preguntar desde **tu** chat de oMLX («¿qué tickets P1 hay?», «pasa el TCK-0004 a en_proceso»). El mismo servidor se puede conectar a **Claude Desktop** o **Claude Code** con esta configuración:

```json
{ "mcpServers": { "tickets": { "command": "/usr/bin/python3", "args": ["/RUTA/ABSOLUTA/A/mcp_tickets.py"] } } }
```

**Seguridad:** oMLX añade las herramientas MCP a todos los chats, pero `server.py` fuerza `tool_choice: "none"` en los chats de la web pública y no expone ningún endpoint `/v1/mcp/*`. Los visitantes no pueden listar ni modificar tickets; solo crear el suyo con el botón. Si el registro falla, el usuario recibe un aviso y puede reintentar.

- El catálogo de servicios, la lista de software autorizado, las prioridades y los equipos resolutores se editan en `contexto.js`.
- Para usar un sistema de tickets real (Jira Service Management, GLPI, Freshdesk…) basta con otro servidor MCP con una herramienta de creación y apuntar `TICKETS_MCP_TOOL` (en `.env`) a ella.

## Configuración (`config.js`)

| Campo | Qué hace |
|---|---|
| `baseUrl` | Dónde está la API. `''` = el mismo `server.py` que sirve la web (recomendado) |
| `assistantName`, `greeting` | Nombre y saludo inicial del asistente |
| `avatar` | Imagen de la cabecera del panel del asistente (p. ej. `img/faena-symbol.png`) |
| `modelLabel` | Nombre del modelo que se muestra en el panel (p. ej. `Faena-Bot`). Vacío = id real del modelo en oMLX |
| `systemPrompt` | Instrucciones de comportamiento del modelo |
| `tickets` | `{ endpoint: '/api/tickets' }` activa el botón «Enviar solicitud». `null` lo desactiva |
| `maxTokens`, `temperature` | Longitud máxima y creatividad de las respuestas |
| `enableThinking` | `true` para que los modelos con razonamiento "piensen" antes de responder (más lento) |
| `maxDocChars` | Máximo de caracteres que se envían de cada documento (el resto se recorta) |
| `maxFileMB` | Tamaño máximo por archivo adjunto |

**Modelo:** se elige solo. Usa el modelo cargado en oMLX; si no hay ninguno, el modelo por defecto del servidor (oMLX lo carga al primer mensaje).

## Contexto del asistente (`contexto.js`)

Define qué puede y debe responder el asistente. Todos los campos son opcionales:

| Campo | Qué hace |
|---|---|
| `identidad` | Quién es el asistente y cuál es su objetivo |
| `tono` | Lista de pautas de estilo |
| `conocimiento` | Texto (Markdown) con toda la información que el asistente puede usar. Es su única fuente de verdad |
| `temasPermitidos` | Temas sobre los que puede hablar |
| `fueraDeTema` | `permitir: false` hace que rechace otros temas con el texto de `respuesta` |
| `reglas` | Obligaciones y prohibiciones (p. ej. "no inventes precios") |
| `preguntas` | Respuestas para preguntas concretas: `si` (frases o palabras clave), `responder` y `fija` |
| `sugerencias` | Botones de preguntas que aparecen al empezar una conversación |

En `preguntas`, `fija: false` le da la respuesta al modelo como guía (él la redacta). `fija: true` responde ese texto exacto al instante, sin consultar al modelo, cuando la pregunta contiene alguna de las palabras de `si` (sin distinguir mayúsculas ni tildes).

Para ver el prompt final que recibe el modelo, abre la consola del navegador y ejecuta `omlxAssistant.systemPrompt()`. Si borras `contexto.js` (o su `<script>` en `index.html`), el asistente vuelve a usar solo el `systemPrompt` de `config.js`.

## Adjuntar imágenes y documentos

Usa el clip del cuadro de texto, arrastra archivos al panel o pega una imagen con Cmd+V.

| Tipo | Cómo se procesa |
|---|---|
| Imágenes (PNG, JPG, WebP, GIF…) | Se reducen a 1536 px como máximo y se envían al modelo. Requiere un modelo de visión (`vlm`, p. ej. Qwen3.5) |
| PDF | Se extrae el texto con pdf.js. Los PDF escaneados (sin texto) no funcionan: envíalos como imagen |
| Word (.docx) | Texto extraído con mammoth.js |
| Excel (.xlsx, .xls) | Cada hoja se convierte a CSV con SheetJS |
| Texto y código (.txt, .md, .csv, .json, .py, …) | Se leen tal cual |

Los lectores de PDF, Word y Excel se descargan de cdnjs solo cuando se usan por primera vez, así que ese primer uso necesita internet. El contenido de los documentos se envía al modelo como texto dentro del mensaje.

## Reutilizar el widget en otra página

```html
<link rel="stylesheet" href="assistant/assistant.css">
<script src="config.js"></script>
<script src="assistant/assistant.js"></script>
```

Desde tu propio código puedes llamar a `window.omlxAssistant.open()`, `.close()`, `.toggle()` o `.reset()`.

## Notas

- La conversación y el estado abierto/cerrado se guardan en el `localStorage` del navegador.
- La API key queda visible en el código del navegador: úsalo solo en local o en una red de confianza.
