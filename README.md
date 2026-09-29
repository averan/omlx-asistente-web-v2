# Faena · Asistente local con oMLX

Página web de demostración con un asistente de chat flotante (esquina inferior derecha) que responde usando el modelo que tengas cargado en **oMLX**.

HTML, CSS y JavaScript puros: sin dependencias ni paso de compilación.

## Estructura

```
index.html              Página demo
css/site.css            Estilos de la página
assistant/assistant.css Estilos del widget (todo bajo .oa-root)
assistant/assistant.js  Lógica del widget (se monta solo en <body>)
config.js               Configuración técnica: URL, modelo, límites…
config.local.js         Tu API key (privado, ignorado por git)
contexto.js             Qué sabe el asistente, de qué habla y cómo responde
img/                    Logo de Faena (símbolo, texto) y favicons
```

## Cómo abrirlo

1. Asegúrate de que oMLX está en marcha en `http://localhost:8000`.
2. Abre la página:

   ```bash
   open index.html
   ```

   O sírvela con un servidor estático (útil si el navegador bloquea algo desde `file://`):

   ```bash
   python3 -m http.server 5174
   ```

   y visita http://localhost:5174

## Primeros pasos

Crea tu configuración privada con la API key de oMLX (este archivo no se sube a git):

```bash
cp config.local.example.js config.local.js
```

Edita `config.local.js` y pon tu clave en `apiKey`. Si tu oMLX no usa API key, puedes dejar el archivo sin crear.

## Configuración (`config.js`)

| Campo | Qué hace |
|---|---|
| `baseUrl` | Dirección del servidor oMLX |
| `apiKey` | API key de oMLX. Va en `config.local.js`, no en `config.js` |
| `assistantName`, `greeting` | Nombre y saludo inicial del asistente |
| `avatar` | Imagen de la cabecera del panel del asistente (p. ej. `img/faena-symbol.png`) |
| `modelLabel` | Nombre del modelo que se muestra en el panel (p. ej. `Faena-Bot`). Vacío = id real del modelo en oMLX |
| `systemPrompt` | Instrucciones de comportamiento del modelo |
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
