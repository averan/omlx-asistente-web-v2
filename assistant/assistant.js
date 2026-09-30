/*
 * Asistente oMLX — widget de chat flotante.
 * Uso: cargar config.js (window.OMLX_ASSISTANT), assistant.css y este archivo.
 * API pública: window.omlxAssistant.open() / .close() / .toggle() / .reset()
 */
(() => {
  const cfg = Object.assign({
    baseUrl: 'http://localhost:8000',
    apiKey: '',
    assistantName: 'Asistente',
    greeting: '¡Hola! ¿En qué puedo ayudarte?',
    systemPrompt: 'Eres un asistente útil y conciso.',
    maxTokens: 1024,
    temperature: 0.7,
    enableThinking: false,
    maxDocChars: 60000,
    maxFileMB: 25,
    modelLabel: '', // nombre visible del modelo; vacío = id real de oMLX
    avatar: '',     // URL de una imagen para la cabecera del panel; vacío = degradado
    tickets: null,  // { endpoint: '/api/tickets' } activa el botón «Enviar solicitud»
  }, window.OMLX_ASSISTANT || {});
  const base = cfg.baseUrl.replace(/\/+$/, '');

  // ---------- almacenamiento (puede fallar en modo privado) ----------
  const KEY = 'omlx-assistant.';
  const store = {
    get(k, d) { try { const v = localStorage.getItem(KEY + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
    set(k, v) { try { localStorage.setItem(KEY + k, JSON.stringify(v)); return true; } catch { return false; } },
  };

  // ---------- estado ----------
  let history = store.get('history', []);
  let model = null;
  let modelType = null;
  let detecting = null;
  let ctrl = null;
  let pending = []; // adjuntos a la espera de enviarse
  const originals = new Map(); // fid -> File original (solo en memoria, para guardarlo como evidencia)
  const modelName = () => cfg.modelLabel || model;

  // ---------- contexto (contexto.js) ----------
  const ctx = window.OMLX_CONTEXT || null;
  const list_ = a => (Array.isArray(a) ? a : a ? [a] : []).filter(Boolean);
  const norm = s => String(s).toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g, '').replace(/[¿?¡!.,;:()"']/g, ' ').replace(/\s+/g, ' ').trim();

  // Construye el prompt de sistema a partir de contexto.js + systemPrompt de config.js
  function buildSystemPrompt() {
    if (!ctx) return cfg.systemPrompt;
    const out = [];
    if (ctx.identidad) out.push(ctx.identidad.trim());
    const bullets = a => list_(a).map(x => `- ${x}`).join('\n');
    if (list_(ctx.tono).length) out.push(`## Tono y estilo\n${bullets(ctx.tono)}`);
    if (ctx.conocimiento?.trim()) out.push(`## Información que conoces\nEsta es tu única fuente de verdad sobre la organización:\n\n${ctx.conocimiento.trim()}`);
    const temas = list_(ctx.temasPermitidos);
    if (temas.length) {
      const fuera = ctx.fueraDeTema || {};
      const regla = fuera.permitir
        ? 'Si te preguntan por otros temas, puedes responder brevemente, pero reconduce la conversación hacia estos temas.'
        : `Si la pregunta NO está relacionada con estos temas, no la respondas y contesta exactamente: "${fuera.respuesta || 'Lo siento, solo puedo ayudarte con temas relacionados con este sitio.'}"`;
      out.push(`## Temas sobre los que puedes responder\n${bullets(temas)}\n\n${regla}`);
    }
    if (list_(ctx.reglas).length) out.push(`## Reglas obligatorias\n${bullets(ctx.reglas)}`);
    const guias = list_(ctx.preguntas).filter(p => p.responder);
    if (guias.length) out.push('## Respuestas para preguntas específicas\nCuando el usuario pregunte algo equivalente a lo siguiente, responde con esa información (puedes redactarla con tus palabras, sin cambiar el fondo):\n\n' +
      guias.map(p => `- Pregunta: ${list_(p.si).map(q => `"${q}"`).join(', ')}\n  Respuesta: ${p.responder}`).join('\n'));
    if (cfg.systemPrompt) out.push(`## Instrucciones adicionales\n${cfg.systemPrompt}`);
    return out.join('\n\n');
  }
  const systemPrompt = buildSystemPrompt();

  // Busca una respuesta fija (fija: true) cuya palabra clave aparezca en la pregunta
  function fixedAnswer(text) {
    const q = ` ${norm(text)} `;
    for (const p of list_(ctx?.preguntas)) {
      if (!p.fija || !p.responder) continue;
      if (list_(p.si).some(k => { const n = norm(k); return n && q.includes(` ${n}`); })) return p.responder;
    }
    return null;
  }

  // Guarda el historial; si no cabe en localStorage (imágenes/documentos grandes), guarda solo los nombres de los adjuntos.
  function saveHistory() {
    if (store.set('history', history)) return;
    const light = history.map(m => m.attachments
      ? { ...m, attachments: m.attachments.map(({ type, name, info }) => ({ type, name, info, stripped: true })) }
      : m);
    store.set('history', light);
  }

  // ---------- DOM ----------
  const ICONS = {
    chat: '<svg class="oa-ic-chat" viewBox="0 0 24 24"><path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z"/></svg>',
    close: '<svg class="oa-ic-close" viewBox="0 0 24 24"><path d="M18 6 6 18M6 6l12 12"/></svg>',
    reset: '<svg viewBox="0 0 24 24"><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/></svg>',
    expand: '<svg class="oa-ic-expand" viewBox="0 0 24 24"><path d="M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7"/></svg>',
    shrink: '<svg class="oa-ic-shrink" viewBox="0 0 24 24"><path d="M4 14h6v6M20 10h-6V4M14 10l7-7M3 21l7-7"/></svg>',
    send: '<svg class="oa-ic-send" viewBox="0 0 24 24"><path d="M12 19V5M5 12l7-7 7 7"/></svg>',
    stop: '<svg class="oa-ic-stop" viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>',
    clip: '<svg viewBox="0 0 24 24"><path d="m21 11-8.5 8.5a5 5 0 0 1-7-7L14 4a3.3 3.3 0 0 1 4.7 4.7l-8.5 8.5a1.7 1.7 0 0 1-2.4-2.4L15.5 7"/></svg>',
    file: '<svg viewBox="0 0 24 24"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5M9 13h6M9 17h4"/></svg>',
  };
  const ACCEPT = 'image/*,.pdf,.docx,.xlsx,.xls,.csv,.tsv,.txt,.md,.markdown,.json,.xml,.html,.htm,.css,.js,.ts,.jsx,.tsx,.py,.java,.c,.h,.cpp,.cs,.go,.rs,.rb,.php,.swift,.kt,.sh,.sql,.yaml,.yml,.toml,.ini,.log,.out,.err,.trace,.tex,.rtf';
  const root = document.createElement('div');
  root.className = 'oa-root';
  root.innerHTML = `
    <section class="oa-panel" role="dialog" aria-modal="false" aria-labelledby="oa-title" id="oa-panel">
      <header class="oa-header">
        <div class="oa-avatar" aria-hidden="true"></div>
        <div class="oa-title">
          <strong id="oa-title"></strong>
          <div class="oa-status"><span class="oa-dot" data-state="checking"></span><span class="oa-status-text">Conectando…</span></div>
        </div>
        <button class="oa-icon-btn oa-expand" type="button" title="Ampliar ventana" aria-label="Ampliar ventana" aria-pressed="false">${ICONS.expand}${ICONS.shrink}</button>
        <button class="oa-icon-btn oa-reset" type="button" title="Nueva conversación" aria-label="Nueva conversación">${ICONS.reset}</button>
        <button class="oa-icon-btn oa-close" type="button" title="Cerrar" aria-label="Cerrar asistente">${ICONS.close.replace('oa-ic-close', '')}</button>
      </header>
      <div class="oa-messages" aria-live="polite"></div>
      <form class="oa-composer">
        <div class="oa-tray" hidden></div>
        <div class="oa-input-wrap">
          <button class="oa-attach" type="button" title="Adjuntar imágenes o documentos" aria-label="Adjuntar archivos">${ICONS.clip}</button>
          <input class="oa-file" type="file" multiple accept="${ACCEPT}" hidden>
          <textarea class="oa-input" rows="1" placeholder="Escribe tu pregunta…" aria-label="Mensaje"></textarea>
          <button class="oa-send" type="submit" aria-label="Enviar" disabled>${ICONS.send}${ICONS.stop}</button>
        </div>
        <div class="oa-footnote">Se ejecuta en local con oMLX · imágenes, PDF, Word, Excel y texto</div>
      </form>
      <div class="oa-resize oa-resize-n" data-dir="n" aria-hidden="true"></div>
      <div class="oa-resize oa-resize-w" data-dir="w" aria-hidden="true"></div>
      <div class="oa-resize oa-resize-nw" data-dir="nw" aria-hidden="true" title="Arrastra para cambiar el tamaño · doble clic para restaurar"></div>
      <div class="oa-drop" aria-hidden="true"><div>${ICONS.clip}<span>Suelta aquí tus archivos</span></div></div>
    </section>
    <button class="oa-launcher" type="button" aria-controls="oa-panel" aria-expanded="false" aria-label="Abrir asistente">
      ${ICONS.chat}${ICONS.close}<span class="oa-dot" data-state="checking"></span>
    </button>`;
  document.body.append(root);

  const $ = s => root.querySelector(s);
  const panel = $('.oa-panel'), launcher = $('.oa-launcher'), list = $('.oa-messages');
  const input = $('.oa-input'), sendBtn = $('.oa-send'), form = $('.oa-composer');
  const tray = $('.oa-tray'), fileInput = $('.oa-file');
  $('#oa-title').textContent = cfg.assistantName;
  if (cfg.avatar) $('.oa-avatar').replaceWith(Object.assign(document.createElement('img'), { className: 'oa-avatar oa-avatar-img', src: cfg.avatar, alt: '' }));

  // ---------- tamaño del panel (arrastrar bordes / ampliar) ----------
  const expandBtn = $('.oa-expand');
  const MIN_W = 320, MIN_H = 380;
  const maxW = () => window.innerWidth - 48, maxH = () => window.innerHeight - 124;
  function applySize() {
    const size = store.get('size', null), expanded = store.get('expanded', false);
    root.classList.toggle('oa-expanded', expanded);
    expandBtn.setAttribute('aria-pressed', String(expanded));
    const label = expanded ? 'Restaurar tamaño' : 'Ampliar ventana';
    expandBtn.title = label; expandBtn.setAttribute('aria-label', label);
    if (size && !expanded) { panel.style.setProperty('--oa-w', size.w + 'px'); panel.style.setProperty('--oa-h', size.h + 'px'); }
    else { panel.style.removeProperty('--oa-w'); panel.style.removeProperty('--oa-h'); }
  }
  expandBtn.addEventListener('click', () => { store.set('expanded', !store.get('expanded', false)); applySize(); scrollToEnd(false); });
  root.querySelectorAll('.oa-resize').forEach(h => {
    h.addEventListener('pointerdown', e => {
      if (e.button !== 0) return;
      e.preventDefault();
      const dir = h.dataset.dir, sx = e.clientX, sy = e.clientY;
      const sw = panel.offsetWidth, sh = panel.offsetHeight;
      if (store.get('expanded', false)) { store.set('expanded', false); root.classList.remove('oa-expanded'); expandBtn.setAttribute('aria-pressed', 'false'); }
      try { h.setPointerCapture(e.pointerId); } catch {}
      root.classList.add('oa-resizing');
      let size = { w: sw, h: sh };
      const move = ev => {
        const w = dir.includes('w') ? sw + (sx - ev.clientX) : sw;
        const hh = dir.includes('n') ? sh + (sy - ev.clientY) : sh;
        size = { w: Math.round(Math.min(Math.max(w, MIN_W), maxW())), h: Math.round(Math.min(Math.max(hh, MIN_H), maxH())) };
        panel.style.setProperty('--oa-w', size.w + 'px');
        panel.style.setProperty('--oa-h', size.h + 'px');
      };
      const up = () => {
        h.removeEventListener('pointermove', move);
        root.classList.remove('oa-resizing');
        store.set('size', size); applySize();
      };
      h.addEventListener('pointermove', move);
      h.addEventListener('pointerup', up, { once: true });
      h.addEventListener('pointercancel', up, { once: true });
    });
    h.addEventListener('dblclick', () => { store.set('size', null); store.set('expanded', false); applySize(); });
  });
  applySize();

  // ---------- abrir / cerrar ----------
  function setOpen(open, { focus = true } = {}) {
    root.classList.toggle('oa-open', open);
    launcher.setAttribute('aria-expanded', String(open));
    launcher.setAttribute('aria-label', open ? 'Cerrar asistente' : 'Abrir asistente');
    store.set('open', open);
    if (open) {
      if (!model) detectModel().catch(() => {}); // el estado ya muestra el error
      scrollToEnd(false);
      autosize();
      if (focus) setTimeout(() => input.focus(), 120);
    } else if (focus && panel.contains(document.activeElement)) {
      launcher.focus();
    }
  }
  const isOpen = () => root.classList.contains('oa-open');
  launcher.addEventListener('click', () => setOpen(!isOpen()));
  $('.oa-close').addEventListener('click', () => setOpen(false));
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && isOpen()) setOpen(false); });

  // ---------- estado de conexión ----------
  function setStatus(state, text) {
    root.querySelectorAll('.oa-dot').forEach(d => { d.dataset.state = state; });
    $('.oa-status-text').textContent = text;
    $('.oa-status').title = text;
  }

  async function api(path, init = {}) {
    const headers = Object.assign({}, init.headers);
    if (cfg.apiKey) headers.Authorization = 'Bearer ' + cfg.apiKey;
    let res;
    try { res = await fetch(base + path, { ...init, headers }); }
    catch (e) {
      if (e.name === 'AbortError') throw e;
      const err = new Error(`No puedo conectar con el servidor del asistente (${base || location.origin}). ¿Está en marcha?`);
      err.network = true; throw err;
    }
    if (!res.ok) {
      let msg = await res.text();
      try { const j = JSON.parse(msg); msg = j.error?.message || (typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail)) || msg; } catch {}
      const err = new Error(res.status === 401 ? 'La API key no es válida (revisa config.js).' : `${msg} (código ${res.status})`);
      err.status = res.status; throw err;
    }
    return res;
  }
  const getJSON = async path => (await api(path)).json();

  // Elige automáticamente el modelo: el cargado > el por defecto > el primero disponible.
  function detectModel() {
    if (detecting) return detecting;
    setStatus('checking', 'Buscando modelo…');
    detecting = (async () => {
      let lastErr, all = [];
      const typeOf = id => all.find(m => m.id === id)?.model_type || null;
      try {
        const s = await getJSON('/v1/models/status');
        all = s.models || [];
        const m = all.find(m => m.loaded && (!m.model_type || /llm|vlm/i.test(m.model_type)));
        if (m) { model = m.id; modelType = m.model_type || null; setStatus('ok', modelName()); return model; }
      } catch (e) { lastErr = e; }
      try {
        const h = await getJSON('/health');
        if (h.default_model) { model = h.default_model; modelType = typeOf(model); setStatus('ok', `${modelName()} · se cargará al preguntar`); return model; }
      } catch (e) { lastErr = e; }
      try {
        const l = await getJSON('/v1/models');
        if (l.data?.[0]) { model = l.data[0].id; modelType = typeOf(model); setStatus('ok', `${modelName()} · se cargará al preguntar`); return model; }
      } catch (e) { lastErr = e; }
      model = null; modelType = null;
      setStatus('error', lastErr?.status === 401 ? 'API key no válida' : 'oMLX no disponible');
      throw lastErr || new Error('oMLX no tiene modelos disponibles.');
    })().finally(() => { detecting = null; });
    return detecting;
  }

  // ---------- Markdown ligero y seguro ----------
  const esc = s => s.replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  function inline(s) {
    return esc(s)
      .replace(/`([^`\n]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
      .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
      .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  }
  function markdown(src) {
    const out = [];
    const parts = src.split(/```/);
    parts.forEach((part, i) => {
      if (i % 2 === 1) { // bloque de código (el primer renglón puede ser el lenguaje)
        const lines = part.replace(/^[\w+-]*\n/, '').replace(/\n[ \t]*$/, '').split('\n');
        const indent = Math.min(...lines.filter(l => l.trim()).map(l => l.match(/^[ \t]*/)[0].length));
        const code = lines.map(l => l.slice(Number.isFinite(indent) ? indent : 0)).join('\n');
        out.push(`<pre><code>${esc(code)}</code></pre>`);
        return;
      }
      let listType = null, para = [];
      const flushPara = () => { if (para.length) { out.push(`<p>${para.map(inline).join('<br>')}</p>`); para = []; } };
      const closeList = () => { if (listType) { out.push(`</${listType}>`); listType = null; } };
      for (const line of part.split('\n')) {
        const ul = line.match(/^\s*[-*•]\s+(.*)$/), ol = line.match(/^\s*\d+[.)]\s+(.*)$/), h = line.match(/^#{1,6}\s+(.*)$/);
        if (ul || ol) {
          flushPara();
          const t = ul ? 'ul' : 'ol';
          if (listType !== t) { closeList(); out.push(`<${t}>`); listType = t; }
          out.push(`<li>${inline((ul || ol)[1])}</li>`);
        } else if (h) { flushPara(); closeList(); out.push(`<h4>${inline(h[1])}</h4>`); }
        else if (!line.trim()) { flushPara(); closeList(); }
        else { closeList(); para.push(line); }
      }
      flushPara(); closeList();
    });
    return out.join('');
  }

  // ---------- render de mensajes ----------
  function scrollToEnd(smooth = true) {
    list.style.scrollBehavior = smooth ? 'smooth' : 'auto';
    list.scrollTop = list.scrollHeight;
  }
  const nearBottom = () => list.scrollHeight - list.scrollTop - list.clientHeight < 60;
  function addBubble(role, content, attachments) {
    const el = document.createElement('div');
    el.className = `oa-msg oa-${role}`;
    if (role === 'user') {
      if (content) el.append(Object.assign(document.createElement('div'), { textContent: content }));
      if (attachments?.length) el.append(attachmentsView(attachments));
    } else el.innerHTML = markdown(content);
    list.append(el);
    return el;
  }
  function renderAll() {
    list.innerHTML = '';
    addBubble('assistant', cfg.greeting);
    history.forEach((m, i) => { const b = addBubble(m.role, m.content, m.attachments); if (m.role === 'assistant') ticketUI(b, i); });
    const sug = list_(ctx?.sugerencias);
    if (!history.length && sug.length) {
      const box = document.createElement('div');
      box.className = 'oa-suggest';
      for (const s of sug) {
        const b = document.createElement('button');
        b.type = 'button'; b.textContent = s;
        b.onclick = () => { if (!ctrl) { input.value = s; form.requestSubmit(); } };
        box.append(b);
      }
      list.append(box);
    }
    scrollToEnd(false);
  }

  // ---------- adjuntos ----------
  const fmtSize = b => b < 1024 ? `${b} B` : b < 1048576 ? `${(b / 1024).toFixed(0)} KB` : `${(b / 1048576).toFixed(1)} MB`;
  const TEXT_EXT = /\.(txt|md|markdown|csv|tsv|json|xml|html?|css|jsx?|tsx?|py|java|c|h|cpp|cs|go|rs|rb|php|swift|kt|sh|sql|ya?ml|toml|ini|log|out|err|trace|tex|rtf)$/i;
  const LOG_EXT = /\.(log|out|err|trace)(\.\d+)?$/i; // en los logs lo relevante suele estar al final
  const CDN = 'https://cdnjs.cloudflare.com/ajax/libs/';
  const LIBS = {
    pdf: CDN + 'pdf.js/3.11.174/pdf.min.js',
    pdfWorker: CDN + 'pdf.js/3.11.174/pdf.worker.min.js',
    docx: CDN + 'mammoth/1.6.0/mammoth.browser.min.js',
    xlsx: CDN + 'xlsx/0.18.5/xlsx.full.min.js',
  };
  const loaded = {};
  function loadScript(src) {
    return loaded[src] ||= new Promise((ok, ko) => {
      const s = Object.assign(document.createElement('script'), { src, async: true });
      s.onload = ok;
      s.onerror = () => { delete loaded[src]; s.remove(); ko(new Error('No se pudo cargar el lector de este formato (necesita internet la primera vez).')); };
      document.head.append(s);
    });
  }

  // Reduce imágenes grandes para no saturar el contexto ni el almacenamiento.
  async function readImage(file) {
    const url = await new Promise((ok, ko) => { const r = new FileReader(); r.onload = () => ok(r.result); r.onerror = () => ko(new Error('No se pudo leer la imagen.')); r.readAsDataURL(file); });
    const img = await new Promise((ok, ko) => { const i = new Image(); i.onload = () => ok(i); i.onerror = () => ko(new Error('Formato de imagen no compatible con este navegador.')); i.src = url; });
    const MAX = 1536, scale = Math.min(1, MAX / Math.max(img.naturalWidth, img.naturalHeight));
    const info = `${img.naturalWidth}×${img.naturalHeight}`;
    if (scale === 1 && file.size < 1.5 * 1048576 && /^data:image\/(png|jpeg|webp|gif)/.test(url)) return { url, info };
    const c = Object.assign(document.createElement('canvas'), { width: Math.round(img.naturalWidth * scale), height: Math.round(img.naturalHeight * scale) });
    const ctx = c.getContext('2d');
    ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, c.width, c.height);
    ctx.drawImage(img, 0, 0, c.width, c.height);
    return { url: c.toDataURL('image/jpeg', 0.85), info };
  }
  async function readPdf(file) {
    await loadScript(LIBS.pdf);
    const lib = window.pdfjsLib;
    lib.GlobalWorkerOptions.workerSrc = LIBS.pdfWorker;
    const pdf = await lib.getDocument({ data: await file.arrayBuffer() }).promise;
    const pages = [];
    for (let i = 1; i <= pdf.numPages; i++) {
      const tc = await (await pdf.getPage(i)).getTextContent();
      pages.push(tc.items.map(it => it.str + (it.hasEOL ? '\n' : ' ')).join('').replace(/[ \t]+\n/g, '\n').trim());
    }
    if (!pages.join('').trim()) throw new Error('El PDF no tiene texto seleccionable (¿es un escaneo?). Envíalo como imagen.');
    return { text: pages.map((t, i) => `[Página ${i + 1}]\n${t}`).join('\n\n'), info: `${pdf.numPages} pág.` };
  }
  async function readDocx(file) {
    await loadScript(LIBS.docx);
    const r = await window.mammoth.extractRawText({ arrayBuffer: await file.arrayBuffer() });
    return { text: r.value.trim() };
  }
  async function readXlsx(file) {
    await loadScript(LIBS.xlsx);
    const wb = window.XLSX.read(await file.arrayBuffer());
    const text = wb.SheetNames.map(n => `[Hoja: ${n}]\n${window.XLSX.utils.sheet_to_csv(wb.Sheets[n]).trim()}`).join('\n\n');
    return { text, info: `${wb.SheetNames.length} hoja${wb.SheetNames.length > 1 ? 's' : ''}` };
  }
  async function readAttachment(file) {
    if (file.size > cfg.maxFileMB * 1048576) throw new Error(`Supera el máximo de ${cfg.maxFileMB} MB.`);
    const name = file.name.toLowerCase();
    if (file.type.startsWith('image/')) return { type: 'image', ...(await readImage(file)) };
    let r;
    if (name.endsWith('.pdf') || file.type === 'application/pdf') r = await readPdf(file);
    else if (name.endsWith('.docx')) r = await readDocx(file);
    else if (/\.xlsx?$/.test(name)) r = await readXlsx(file);
    else if (file.type.startsWith('text/') || TEXT_EXT.test(name) || LOG_EXT.test(name) || file.type === 'application/json') r = { text: (await file.text()).trim() };
    else throw new Error('Formato no compatible. Usa imágenes, PDF, Word (.docx), Excel o archivos de texto.');
    if (!r.text) throw new Error('El documento está vacío.');
    const chars = r.text.length;
    const n = cfg.maxDocChars.toLocaleString('es'), total = chars.toLocaleString('es');
    if (chars > cfg.maxDocChars && LOG_EXT.test(name)) r.text = `[… log recortado: se omitió el inicio; se envían los últimos ${n} de ${total} caracteres]\n\n` + r.text.slice(-cfg.maxDocChars);
    else if (chars > cfg.maxDocChars) r.text = r.text.slice(0, cfg.maxDocChars) + `\n\n[… documento recortado: se enviaron ${n} de ${total} caracteres]`;
    return { type: 'doc', text: r.text, info: [r.info, chars > cfg.maxDocChars ? 'recortado' : (chars < 1000 ? `${chars} caract.` : `${Math.round(chars / 1000)}k caract.`)].filter(Boolean).join(' · ') };
  }

  function addFiles(files) {
    for (const file of files) {
      const item = { id: Math.random().toString(36).slice(2), name: file.name || 'imagen-pegada.png', size: file.size, status: 'loading', file };
      pending.push(item);
      readAttachment(file)
        .then(r => Object.assign(item, r, { status: 'ready' }))
        .catch(e => Object.assign(item, { status: 'error', error: e.message }))
        .finally(() => { renderTray(); updateSendState(); });
    }
    renderTray(); updateSendState();
  }
  function chip(att, onRemove) {
    const el = document.createElement('div');
    el.className = `oa-chip oa-chip-${att.type || 'doc'}` + (att.status === 'error' ? ' oa-chip-error' : '') + (att.status === 'loading' ? ' oa-chip-loading' : '');
    if (att.type === 'image' && att.url) {
      el.append(Object.assign(document.createElement('img'), { src: att.url, alt: att.name }));
    } else {
      const ic = document.createElement('span'); ic.className = 'oa-chip-icon'; ic.innerHTML = ICONS.file; el.append(ic);
    }
    const txt = document.createElement('span'); txt.className = 'oa-chip-text';
    const sub = att.status === 'loading' ? 'Leyendo…' : att.status === 'error' ? att.error : att.stripped ? 'no se guardó al recargar' : (att.info || (att.size ? fmtSize(att.size) : ''));
    txt.innerHTML = '<b></b><small></small>';
    txt.firstChild.textContent = att.name; txt.lastChild.textContent = sub;
    el.title = `${att.name}${sub ? ' — ' + sub : ''}`;
    el.append(txt);
    if (onRemove) {
      const x = document.createElement('button');
      x.type = 'button'; x.className = 'oa-chip-x'; x.setAttribute('aria-label', `Quitar ${att.name}`); x.textContent = '×';
      x.onclick = onRemove; el.append(x);
    }
    return el;
  }
  function renderTray() {
    tray.innerHTML = '';
    tray.hidden = !pending.length;
    for (const att of pending) tray.append(chip(att, () => { pending = pending.filter(p => p !== att); renderTray(); updateSendState(); input.focus(); }));
  }
  function attachmentsView(atts) {
    const box = document.createElement('div'); box.className = 'oa-atts';
    for (const a of atts) box.append(chip(a));
    return box;
  }

  // Convierte un mensaje del historial al formato de la API (texto + documentos + imágenes).
  function toApiMessage(m) {
    if (m.role !== 'user' || !m.attachments?.length) return { role: m.role, content: m.content };
    const docs = m.attachments.filter(a => a.type === 'doc' || !a.url);
    const imgs = m.attachments.filter(a => a.type === 'image' && a.url);
    let text = docs.map(d => d.text
      ? `<documento nombre="${d.name}">\n${d.text}\n</documento>`
      : `(El usuario adjuntó "${d.name}", pero su contenido ya no está disponible.)`).join('\n\n');
    const question = m.content || (imgs.length && !docs.length ? 'Describe la imagen adjunta.' : 'Resume el contenido adjunto.');
    text = text ? `${text}\n\n${question}` : question;
    if (!imgs.length) return { role: 'user', content: text };
    return { role: 'user', content: [{ type: 'text', text }, ...imgs.map(a => ({ type: 'image_url', image_url: { url: a.url } }))] };
  }
  function showError(message, retry) {
    const el = document.createElement('div');
    el.className = 'oa-msg oa-error';
    el.setAttribute('role', 'alert');
    el.textContent = message;
    if (retry) {
      const b = document.createElement('button');
      b.type = 'button'; b.textContent = 'Reintentar';
      b.onclick = () => { el.remove(); retry(); };
      el.append(document.createElement('br'), b);
    }
    list.append(el); scrollToEnd();
  }

  // ---------- solicitudes (tickets) ----------
  // El asistente presenta la solicitud con el encabezado TICKET_MARK y campos "- **Campo:** valor".
  // El widget la lee, muestra «Enviar solicitud» y la envía al servidor, que asigna el número TCK-…
  const TICKET_MARK = 'solicitud lista para enviar';
  const TICKET_KEYS = {
    'tipo': 'tipo', 'titulo': 'titulo', 'categoria': 'categoria', 'descripcion': 'descripcion',
    'nombre': 'nombre', 'nombre completo': 'nombre', 'correo': 'correo', 'correo electronico': 'correo', 'email': 'correo', 'mail': 'correo',
    'telefono': 'telefono', 'telefono de contacto': 'telefono', 'prioridad': 'prioridad',
    'equipo resolutor': 'equipo', 'equipo': 'equipo', 'evidencias': 'evidencias',
  };
  const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
  function parseTicket(md) {
    if (!norm(md).includes(TICKET_MARK)) return null;
    const t = {};
    for (const line of md.split('\n')) {
      const m = line.match(/^\s*(?:[-*•]\s*)?(?:\*\*)?([^:*\n]{3,30}?)(?:\*\*)?\s*:\s*(?:\*\*)?\s*(.+?)\s*$/);
      const key = m && TICKET_KEYS[norm(m[1])];
      if (key && !t[key]) t[key] = m[2].replace(/\*\*/g, '').replace(/^\[|\]$/g, '').trim();
    }
    return t;
  }
  function ticketProblems(t) {
    const faltan = [['titulo', 'título'], ['descripcion', 'descripción'], ['nombre', 'nombre'], ['correo', 'correo']]
      .filter(([k]) => !t[k] || /^no informad/i.test(t[k])).map(([, n]) => n);
    if (faltan.length) return `Faltan datos obligatorios (${faltan.join(', ')}): complétalos en la conversación.`;
    if (!EMAIL_RE.test(t.correo)) return 'El correo no parece válido: dime el correcto.';
    return '';
  }
  // Pone la barra de acciones (o la marca de «enviada») bajo la última solicitud presentada
  function ticketUI(bubble, index) {
    if (!cfg.tickets?.endpoint) return;
    const msg = history[index];
    const t = msg && parseTicket(msg.content);
    if (!t) return;
    const bar = document.createElement('div');
    bar.className = 'oa-ticket-bar';
    if (msg.ticketId) {
      bar.classList.add('oa-ticket-done');
      bar.innerHTML = '<span class="oa-ticket-sent"></span>';
      bar.firstChild.textContent = `✓ Enviada · ${msg.ticketId}`;
      bubble.append(bar);
      return;
    }
    const laterTicket = history.slice(index + 1).some(m => m.role === 'assistant' && (m.ticketId || parseTicket(m.content)));
    if (laterTicket) return; // solo la solicitud más reciente se puede enviar
    list.querySelectorAll('.oa-ticket-bar:not(.oa-ticket-done)').forEach(el => el.remove());
    const problem = ticketProblems(t);
    bar.innerHTML = `<button type="button" class="oa-ticket-send">Enviar solicitud</button><button type="button" class="oa-ticket-fix">Corregir</button><span class="oa-ticket-status" role="status"></span>`;
    const sendB = bar.querySelector('.oa-ticket-send'), status = bar.querySelector('.oa-ticket-status');
    if (problem) { sendB.disabled = true; status.textContent = problem; }
    bar.querySelector('.oa-ticket-fix').onclick = () => { input.placeholder = 'Dime qué quieres corregir…'; input.focus(); };
    sendB.onclick = () => submitTicket(t, index, bar);
    bubble.append(bar);
  }
  // Reúne los archivos adjuntos de la conversación para guardarlos como evidencia del ticket.
  // Usa el original si sigue en memoria; si no (p. ej. tras recargar), la imagen reducida o el texto extraído.
  const EVIDENCE_IMAGE = /^image\/(png|jpeg|gif|webp)$/;
  const MAX_EVIDENCE = 10 * 1048576, MAX_EVIDENCE_TOTAL = 25 * 1048576;
  const toBase64 = blob => new Promise((ok, ko) => { const r = new FileReader(); r.onload = () => ok(String(r.result).split(',')[1] || ''); r.onerror = ko; r.readAsDataURL(blob); });
  async function evidenceFiles(index) {
    const archivos = [], omitidos = [];
    let total = 0;
    for (const m of history.slice(0, index + 1)) {
      for (const a of (m.role === 'user' && m.attachments) || []) {
        const file = originals.get(a.fid);
        let nombre = a.name, contenido = null;
        if (file && file.size <= MAX_EVIDENCE && (a.type !== 'image' || EVIDENCE_IMAGE.test(file.type))) contenido = await toBase64(file);
        else if (a.type === 'image' && a.url) { contenido = a.url.split(',')[1]; if (!/\.(jpe?g|png|gif|webp)$/i.test(nombre)) nombre += '.jpg'; }
        else if (a.text) { contenido = await toBase64(new Blob([a.text], { type: 'text/plain' })); nombre += ' (texto extraído).txt'; }
        const size = contenido ? contenido.length * 3 / 4 : 0;
        if (!contenido || archivos.length >= 10 || total + size > MAX_EVIDENCE_TOTAL) { omitidos.push(a.name); continue; }
        total += size;
        archivos.push({ nombre, contenido_base64: contenido });
      }
    }
    return { archivos, omitidos };
  }
  async function submitTicket(t, index, bar) {
    if (ctrl) return;
    const buttons = bar.querySelectorAll('button'), status = bar.querySelector('.oa-ticket-status');
    buttons.forEach(b => { b.disabled = true; });
    const convo = history;
    try {
      status.textContent = 'Preparando evidencias…';
      const { archivos, omitidos } = await evidenceFiles(index);
      status.textContent = archivos.length ? `Enviando solicitud y ${archivos.length} archivo(s)…` : 'Enviando…';
      const res = await api(cfg.tickets.endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          ...t,
          // la lista de evidencias sale de los archivos reales, no de lo que haya escrito el modelo
          evidencias: archivos.length ? archivos.map(f => f.nombre).join(', ') : (omitidos.length ? omitidos.join(', ') : 'ninguna'),
          conversacion: history.slice(0, index + 1).map(m => ({ role: m.role, content: m.content, adjuntos: (m.attachments || []).map(a => a.name) })),
          archivos,
        }),
      });
      const { id, adjuntos = 0, adjuntos_fallidos: fallidos = [] } = await res.json();
      if (history !== convo) return;
      history[index].ticketId = id;
      const noGuardados = [...omitidos, ...fallidos];
      const note = `✅ **Solicitud enviada.** Tu número de ticket es **${id}**. La Mesa de Ayuda te contactará en ${t.correo}.`
        + (adjuntos ? ` Se guardaron ${adjuntos} archivo(s) como evidencia.` : '')
        + (noGuardados.length ? ` No se pudieron guardar: ${noGuardados.join(', ')}.` : '');
      history.push({ role: 'assistant', content: note });
      saveHistory();
      bar.className = 'oa-ticket-bar oa-ticket-done';
      bar.innerHTML = '<span class="oa-ticket-sent"></span>';
      bar.firstChild.textContent = `✓ Enviada · ${id}`;
      addBubble('assistant', note);
      input.placeholder = 'Escribe tu pregunta…';
      scrollToEnd();
    } catch (e) {
      buttons.forEach(b => { b.disabled = false; });
      status.textContent = e.network ? 'No se pudo conectar con el servidor. Inténtalo de nuevo.' : e.message;
    }
  }

  // ---------- streaming SSE ----------
  async function readSSE(res, onData) {
    const reader = res.body.getReader(), dec = new TextDecoder();
    let buf = '';
    const flush = block => {
      const data = block.split(/\r?\n/).filter(l => l.startsWith('data:')).map(l => l.slice(5).replace(/^ /, '')).join('\n');
      if (!data || data === '[DONE]') return;
      try { onData(JSON.parse(data)); } catch (e) { if (!(e instanceof SyntaxError)) throw e; }
    };
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let m;
      while ((m = /\r?\n\r?\n/.exec(buf))) { flush(buf.slice(0, m.index)); buf = buf.slice(m.index + m[0].length); }
    }
    if (buf.trim()) flush(buf);
  }

  // ---------- enviar ----------
  function setBusy(on) {
    root.classList.toggle('oa-busy', on);
    sendBtn.setAttribute('aria-label', on ? 'Detener' : 'Enviar');
    updateSendState();
  }
  function updateSendState() {
    const loading = pending.some(p => p.status === 'loading');
    const ready = pending.some(p => p.status === 'ready');
    sendBtn.disabled = !ctrl && (loading || (!input.value.trim() && !ready));
  }

  async function send(text, attachments = [], userBubble) {
    if (ctrl) return;
    ctrl = new AbortController();
    setBusy(true);
    userBubble ||= addBubble('user', text, attachments);
    history.push(attachments.length ? { role: 'user', content: text, attachments } : { role: 'user', content: text });
    saveHistory();
    const convo = history;

    const bubble = document.createElement('div');
    bubble.className = 'oa-msg oa-assistant';
    bubble.innerHTML = '<span class="oa-typing" aria-label="Escribiendo"><i></i><i></i><i></i></span>';
    list.append(bubble); scrollToEnd();

    let content = '', frame = 0, stopped = false;
    const paint = () => {
      frame = 0;
      const stick = nearBottom();
      bubble.innerHTML = markdown(content);
      if (stick) scrollToEnd(false);
    };
    try {
      if (!model) await detectModel();
      const res = await api('/v1/chat/completions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        signal: ctrl.signal,
        body: JSON.stringify({
          model,
          messages: [{ role: 'system', content: systemPrompt }, ...history.map(toApiMessage)],
          stream: true,
          max_tokens: cfg.maxTokens,
          temperature: cfg.temperature,
          enable_thinking: cfg.enableThinking,
        }),
      });
      setStatus('ok', modelName());
      await readSSE(res, j => {
        if (j.error) throw new Error(j.error.message || 'Error del modelo');
        if (j.model === 'keepalive') return;
        const piece = j.choices?.[0]?.delta?.content;
        if (piece) { content += piece; if (!frame) frame = requestAnimationFrame(paint); }
      });
    } catch (e) {
      if (e.name === 'AbortError') stopped = true;
      else {
        cancelAnimationFrame(frame);
        bubble.remove();
        if (history !== convo) return;
        history.pop();
        saveHistory();
        if (e.network) setStatus('error', 'oMLX no disponible');
        if (e.status === 404 || e.status === 400) model = null; // el modelo pudo descargarse: re-detectar
        showError(e.message, () => { userBubble.remove(); send(text, attachments); });
        return;
      }
    } finally {
      ctrl = null;
      setBusy(false);
    }
    cancelAnimationFrame(frame);
    if (history !== convo) return; // se inició una conversación nueva mientras respondía
    content = content.replace(/^\s+/, '');
    if (!content) {
      bubble.innerHTML = `<em>${stopped ? 'Detenido.' : 'El modelo no devolvió texto. Prueba a aumentar maxTokens en config.js.'}</em>`;
      if (!stopped) { history.pop(); saveHistory(); return; }
      content = '_(detenido)_';
    } else {
      bubble.innerHTML = markdown(content);
      if (stopped) bubble.insertAdjacentHTML('beforeend', '<div class="oa-meta">Detenido</div>');
    }
    history.push({ role: 'assistant', content });
    saveHistory();
    ticketUI(bubble, history.length - 1);
    scrollToEnd();
  }

  form.addEventListener('submit', e => {
    e.preventDefault();
    if (ctrl) { ctrl.abort(); return; }
    const text = input.value.trim();
    if (pending.some(p => p.status === 'loading')) return;
    const ready = pending.filter(p => p.status === 'ready');
    if (!text && !ready.length) return;
    if (ready.some(a => a.type === 'image') && modelType && !/vlm/i.test(modelType)) {
      showError(`El modelo ${modelName()} solo entiende texto y no puede ver imágenes. Carga un modelo de visión (VLM) en oMLX o quita las imágenes.`);
      return;
    }
    const attachments = ready.map(({ id, file, type, name, info, url, text }) => {
      originals.set(id, file);
      return type === 'image' ? { fid: id, type, name, info, url } : { fid: id, type, name, info, text };
    });
    pending = []; renderTray();
    input.value = ''; autosize(); updateSendState();
    list.querySelector('.oa-suggest')?.remove();
    const fixed = !attachments.length && fixedAnswer(text);
    if (fixed) { // respuesta predefinida en contexto.js: no se consulta al modelo
      addBubble('user', text);
      addBubble('assistant', fixed).dataset.fixed = '';
      history.push({ role: 'user', content: text }, { role: 'assistant', content: fixed });
      saveHistory(); scrollToEnd();
      return;
    }
    send(text, attachments);
  });

  // adjuntar: botón, arrastrar y soltar, pegar
  $('.oa-attach').addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', () => { addFiles([...fileInput.files]); fileInput.value = ''; input.focus(); });
  input.addEventListener('paste', e => {
    const files = [...(e.clipboardData?.files || [])];
    if (files.length) { e.preventDefault(); addFiles(files); }
  });
  let dragDepth = 0;
  const hasFiles = e => [...(e.dataTransfer?.types || [])].includes('Files');
  panel.addEventListener('dragenter', e => { if (!hasFiles(e)) return; e.preventDefault(); dragDepth++; root.classList.add('oa-dragging'); });
  panel.addEventListener('dragover', e => { if (hasFiles(e)) e.preventDefault(); });
  panel.addEventListener('dragleave', () => { if (--dragDepth <= 0) { dragDepth = 0; root.classList.remove('oa-dragging'); } });
  panel.addEventListener('drop', e => {
    if (!hasFiles(e)) return;
    e.preventDefault(); dragDepth = 0; root.classList.remove('oa-dragging');
    addFiles([...e.dataTransfer.files]); input.focus();
  });
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); }
  });
  function autosize() {
    if (!input.value) { input.style.height = ''; return; } // vacío: alto natural de una línea
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 140) + 'px';
  }
  window.addEventListener('resize', autosize);
  input.addEventListener('input', () => { autosize(); updateSendState(); });

  function reset() {
    ctrl?.abort();
    history = [];
    pending = []; renderTray(); updateSendState();
    input.value = ''; autosize();
    saveHistory();
    renderAll();
    input.focus();
  }
  $('.oa-reset').addEventListener('click', reset);

  // ---------- inicio ----------
  renderAll();
  setOpen(store.get('open', false), { focus: false });
  detectModel().catch(() => {});

  window.omlxAssistant = { open: () => setOpen(true), close: () => setOpen(false), toggle: () => setOpen(!isOpen()), reset, systemPrompt: () => systemPrompt };
})();
