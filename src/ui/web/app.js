// Cara de granola-local. Toda la lógica vive en el sidecar de Python; esto pinta lo
// que llega por el WebSocket y manda de vuelta lo que el usuario pide.

const PORT = new URLSearchParams(location.search).get("port") || "8765";
const SPEAKER = { mic: "Yo", sys: "Ellos" };
const MY_LANG = { es: "español", en: "inglés" };

const el = (id) => document.getElementById(id);
const stream = el("stream");

let socket = null;
let running = false;
let sessionId = null;
let viewing = null; // sesión que se está mirando, si no es la que graba
let templates = []; // plantillas de config/prompts.yaml, para los botones al vuelo
const selected = new Map(); // "track-index" -> {track, turn_index}

// ---------------------------------------------------------------- conexión

function connect() {
  socket = new WebSocket(`ws://127.0.0.1:${PORT}`);

  socket.onopen = () => {
    el("dot").classList.add("on");
    el("status").textContent = "listo";
  };
  socket.onclose = () => {
    el("dot").classList.remove("on", "rec");
    el("status").textContent = "sin sidecar — reintentando…";
    setTimeout(connect, 1500);
  };
  socket.onmessage = (event) => handle(JSON.parse(event.data));
}

function send(message) {
  if (socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(message));
}

function handle(msg) {
  switch (msg.type) {
    case "state": return onState(msg);
    case "status": return (el("status").textContent = msg.message);
    case "segment": return onSegment(msg);
    case "translation": return onTranslation(msg);
    case "prompts": return onPrompts(msg);
    case "answers": return onAnswers(msg);
    case "sessions": return onSessions(msg.rows);
    case "transcript": return onTranscript(msg);
    case "search_result": return onResults(msg.rows);
    case "answer_start": return onAnswerStart(msg);
    case "answer_chunk": return onAnswerChunk(msg);
    case "answer": return onAnswer(msg);
    case "error": {
      if (pendingCard) {
        pendingCard.remove();
        pendingCard = null;
        countAnswers();
      }
      return toast(msg.message);
    }
  }
}

// ---------------------------------------------------------------- transcripción

function key(track, index) { return `${track}-${index}`; }

function bubbleFor(track, index) {
  const id = key(track, index);
  let node = document.getElementById(id);
  if (node) return node;

  node = document.createElement("div");
  node.id = id;
  node.className = `bubble ${track}`;
  node.innerHTML = `<div class="tools"></div>
                    <div class="who"><b>${SPEAKER[track] || track}</b><span class="t"></span></div>
                    <div class="text"></div><div class="trad"></div>`;
  node.onclick = (event) => toggleSelect(node, track, index, event);
  buildTools(node, track, index);
  stream.appendChild(node);
  el("empty").classList.add("hidden");
  return node;
}

// Las acciones viven encima de cada frase y aparecen al pasar el mouse. Es donde está
// mirando el usuario: obligarlo a ir hasta un panel lateral en medio de una reunión es
// pedirle que deje de escuchar.
function buildTools(node, track, index) {
  const tools = node.querySelector(".tools");
  tools.innerHTML = "";

  const pick = document.createElement("button");
  pick.className = "pick";
  pick.textContent = "✓";
  pick.title = "Sumar esta frase a la selección";
  pick.onclick = (event) => { event.stopPropagation(); toggleSelect(node, track, index, event); };
  tools.appendChild(pick);

  if (templates.length) {
    const sep = document.createElement("div");
    sep.className = "sep";
    tools.appendChild(sep);
  }

  for (const template of templates.filter((t) => !t.usa_respuesta)) {
    const button = document.createElement("button");
    button.textContent = template.corto || template.nombre;
    button.title = template.descripcion || "";
    button.onclick = (event) => {
      event.stopPropagation();
      // Si la frase ya es parte de una selección, la acción va sobre toda la selección.
      // Si no, va sobre esta sola, sin tocar lo que hubiera elegido antes.
      const keys = selected.has(key(track, index))
        ? [...selected.values()]
        : [{ track, turn_index: index }];
      askKeys(template.id, keys);
    };
    tools.appendChild(button);
  }
}

function onSegment(msg) {
  const node = bubbleFor(msg.track, msg.turn_index);
  node.querySelector(".text").textContent = msg.text;
  node.querySelector(".t").textContent = `${msg.t_start.toFixed(1)}s`;
  node.classList.toggle("partial", !msg.final);
  const pane = el("stream-pane");
  const atBottom = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 120;
  if (atBottom) pane.scrollTop = pane.scrollHeight;
}

function onTranslation(msg) {
  bubbleFor(msg.track, msg.turn_index).querySelector(".trad").textContent = msg.text;
}

function onTranscript(msg) {
  viewing = msg.session_id;
  clearStream();
  for (const row of msg.rows) {
    const node = bubbleFor(row.track, row.turn_index);
    node.querySelector(".text").textContent = row.text;
    node.querySelector(".t").textContent = `${row.t_start.toFixed(1)}s`;
    node.querySelector(".trad").textContent = row.text_trad || "";
    node.classList.toggle("partial", row.status !== "confirmed");
  }
}

function clearStream() {
  stream.innerHTML = "";
  selected.clear();
  lastClicked = null;
  renderSelection();
  el("empty").classList.toggle("hidden", false);
}

// ---------------------------------------------------------------- selección

let lastClicked = null;

function toggleSelect(node, track, index, event) {
  // Shift extiende desde la última burbuja tocada: elegir un tramo de diálogo de a una
  // es lo que uno hace más seguido, y de a un clic es tedioso en medio de una reunión.
  if (event && event.shiftKey && lastClicked) {
    const nodes = [...stream.children];
    const from = nodes.indexOf(lastClicked);
    const to = nodes.indexOf(node);
    if (from !== -1 && to !== -1) {
      for (const n of nodes.slice(Math.min(from, to), Math.max(from, to) + 1)) select(n, true);
      lastClicked = node;
      return renderSelection();
    }
  }
  select(node, !selected.has(key(track, index)));
  lastClicked = node;
  renderSelection();
}

function select(node, on) {
  const [track, index] = [node.className.includes("mic") ? "mic" : "sys", Number(node.id.split("-")[1])];
  const id = key(track, index);
  if (on) selected.set(id, { track, turn_index: index });
  else selected.delete(id);
  node.classList.toggle("sel", on);
}

function renderSelection() {
  const n = selected.size;
  el("actionbar").classList.toggle("hidden", n === 0);
  if (!n) return;
  el("selcount").textContent = `${n} frase${n > 1 ? "s" : ""}`;
  const text = [...selected.keys()]
    .map((id) => document.getElementById(id)?.querySelector(".text").textContent || "")
    .join(" · ");
  el("selpreview").textContent = text;
  el("selpreview").title = text;
}

function clearSelection() {
  for (const id of selected.keys()) document.getElementById(id)?.classList.remove("sel");
  selected.clear();
  lastClicked = null;
  renderSelection();
}

el("clear").onclick = clearSelection;

// La acción más común en una reunión es responder a lo último que dijeron. Un clic.
el("reply-last").onclick = () => {
  const bubbles = [...stream.children].filter((n) => n.classList.contains("sys"));
  const last = bubbles[bubbles.length - 1];
  if (!last) return toast("Todavía no dijeron nada.");
  clearSelection();
  select(last, true);
  lastClicked = last;
  renderSelection();
  last.scrollIntoView({ block: "center" });
  const first = el("prompts").querySelector("button");
  if (first) first.click();
};

document.addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
    el("prompts").querySelector("button")?.click();
  }
  if (event.key === "Escape") clearSelection();
});

// ---------------------------------------------------------------- prompts

function onPrompts(msg) {
  templates = msg.templates;
  const box = el("prompts");
  box.innerHTML = "";
  for (const t of msg.templates.filter((x) => !x.usa_respuesta)) {
    const button = document.createElement("button");
    button.textContent = t.nombre;
    button.title = t.descripcion || "";
    button.onclick = () => ask(t.id);
    box.appendChild(button);
  }
  // Las plantillas llegan al conectar, que puede ser después de pintar burbujas.
  for (const node of stream.children) {
    buildTools(node, node.classList.contains("mic") ? "mic" : "sys", Number(node.id.split("-")[1]));
  }

  const cli = el("cli");
  cli.innerHTML = "";
  for (const c of msg.clis) {
    const option = document.createElement("option");
    option.value = c.id;
    // Que se vea antes de mandar nada si el texto sale de la máquina.
    option.textContent = `${c.id} · ${c.local ? "local" : "remota"}${c.installed ? "" : " (no instalada)"}`;
    option.disabled = !c.installed;
    cli.appendChild(option);
  }
  const firstUsable = msg.clis.find((c) => c.installed);
  if (firstUsable) cli.value = firstUsable.id;
}

function ask(template) {
  if (!selected.size) return toast("Elegí al menos una frase.");
  askKeys(template, [...selected.values()]);
}

function askKeys(template, keys, extra = {}) {
  if (!keys.length) return toast("Elegí al menos una frase.");
  send({
    type: "ask",
    template,
    // Ampliar trae su propia CLI desde la plantilla; el desplegable sólo manda cuando
    // el usuario lo tocó a mano.
    cli: extra.respuesta ? null : el("cli").value || null,
    idioma: el("idioma").value,
    // En qué idioma leo yo. Es lo que usan traducir, explicar y resumir.
    propio: MY_LANG[el("lang-mic").value] || "español",
    session_id: viewing || sessionId,
    instrucciones: el("instrucciones").value,
    keys,
    ...extra,
  });
}

// ---------------------------------------------------------------- respuestas

let pendingCard = null;

/** Pinta el texto de una respuesta con el poco markdown que los modelos usan.
 *
 * No es un renderizador de markdown: es negrita, código y viñetas, que es lo que
 * aparece. Todo pasa antes por `escapeHtml`, así que el texto del modelo nunca llega
 * al DOM como etiquetas.
 */
function renderAnswerText(node, text) {
  node.innerHTML = "";
  const lines = text.split("\n");
  let list = null;

  const inline = (line) =>
    escapeHtml(line)
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/`([^`]+)`/g, "<code>$1</code>");

  for (const raw of lines) {
    const line = raw.trimEnd();
    const bullet = line.match(/^\s*[*-]\s+(.*)$/);
    if (bullet) {
      if (!list) {
        list = document.createElement("ul");
        node.appendChild(list);
      }
      const item = document.createElement("li");
      item.innerHTML = inline(bullet[1]);
      list.appendChild(item);
      continue;
    }
    list = null;
    if (!line.trim()) continue;
    const paragraph = document.createElement("p");
    paragraph.innerHTML = inline(line);
    node.appendChild(paragraph);
  }
}

function templateName(id) {
  const found = templates.find((t) => t.id === id);
  return found ? found.corto || found.nombre : id;
}

function metaHtml(data) {
  const who = data.local
    ? `${data.cli} · local`
    : `<span class="remote">${data.cli} · remota</span>`;
  const when = data.elapsed !== undefined
    ? `${data.elapsed.toFixed(1)}s`
    : data.created_at
      ? new Date(data.created_at * 1000).toLocaleTimeString("es", { hour: "2-digit", minute: "2-digit" })
      : "escribiendo…";
  const lang = data.idioma || data.lang || "";
  return `<span class="tpl">${templateName(data.template)}</span>
          <span>${who}${lang ? ` · en ${lang}` : ""} · ${when}</span>`;
}

/** Crea la tarjeta de una respuesta y la pone arriba de todo. */
function answerCard(data) {
  const card = document.createElement("div");
  card.className = "answer fresh";
  card.innerHTML = `<div class="meta"></div><div class="quote"></div>
                    <div class="body"></div><div class="acciones"></div>`;
  card.querySelector(".meta").innerHTML = metaHtml(data);
  card.querySelector(".quote").textContent = data.fragmento || "";
  renderAnswerText(card.querySelector(".body"), data.text || "");
  card.dataset.turnos = data.turnos || JSON.stringify(data.keys || []);
  card.dataset.id = data.id || "";
  card.dataset.parent = data.parent_id || "";

  // Dos órdenes a la vez, porque son dos cosas distintas: lo más nuevo va arriba para no
  // perderlo, pero una ampliación va pegada debajo de la respuesta que amplía. Si la
  // ampliación se agregara arriba de todo, empujaría hacia abajo justo el texto que se
  // está leyendo mientras crece.
  const box = el("answers");
  const parent = data.parent_id ? box.querySelector(`[data-id="${data.parent_id}"]`) : null;
  if (parent) {
    let anchor = parent;
    const same = (node) => node && node.dataset.parent === String(data.parent_id);
    while (same(anchor.nextElementSibling)) {
      anchor = anchor.nextElementSibling;
    }
    anchor.after(card);
  } else {
    box.prepend(card);
    box.scrollTop = 0;
  }

  el("answers-empty").classList.add("hidden");
  return card;
}

/** Los botones que amplían: sólo tienen sentido con una respuesta ya escrita. */
function fillActions(card) {
  const box = card.querySelector(".acciones");
  box.innerHTML = "";
  for (const template of templates.filter((t) => t.usa_respuesta)) {
    const button = document.createElement("button");
    button.textContent = template.corto || template.nombre;
    button.title = template.descripcion || "";
    button.onclick = () => {
      let keys = [];
      try {
        keys = JSON.parse(card.dataset.turnos || "[]");
      } catch (error) {
        keys = [];
      }
      if (!keys.length) return toast("Esa respuesta no guardó a qué frases contestaba.");
      askKeys(template.id, keys, {
        respuesta: card.querySelector(".body").textContent,
        parent_id: Number(card.dataset.id) || null,
      });
    };
    box.appendChild(button);
  }
}

function onAnswerStart(msg) {
  for (const card of el("answers").children) card.classList.remove("fresh");
  pendingCard = answerCard(msg);
  pendingCard.classList.add("pending");
  pendingCard.querySelector(".body").textContent = "pensando…";
  pendingCard.dataset.streaming = "";
}

function onAnswerChunk(msg) {
  if (!pendingCard) return;
  const body = pendingCard.querySelector(".body");
  if (pendingCard.dataset.streaming === "") {
    body.textContent = "";
    pendingCard.dataset.streaming = "1";
  }
  body.textContent += msg.text;
}

function expansionOf(templateId) {
  const found = templates.find((t) => t.id === templateId);
  return found && found.ampliar_con ? found.ampliar_con : null;
}

function onAnswer(msg) {
  const card = pendingCard || answerCard(msg);
  card.classList.remove("pending");
  card.querySelector(".meta").innerHTML = metaHtml(msg);
  card.querySelector(".quote").textContent = msg.fragmento || "";
  // Mientras llega, el texto se pinta crudo; al terminar se rearma con formato.
  renderAnswerText(card.querySelector(".body"), msg.text);
  card.dataset.turnos = msg.turnos || JSON.stringify(msg.keys || []);
  card.dataset.id = msg.id || "";
  fillActions(card);
  pendingCard = null;
  countAnswers();

  // La ampliación arranca sola apenas está la corta. No se lanzan las dos a la vez a
  // propósito: ollama atiende una generación por vez, así que pedir la larga en
  // paralelo retrasaría justo la que se quiere leer ya. Y la larga necesita la corta
  // como entrada. Mientras se lee la de arriba, la de abajo se está escribiendo.
  const next = msg.parent_id ? null : expansionOf(msg.template);
  if (next && el("auto-ampliar").checked) {
    let keys = [];
    try {
      keys = JSON.parse(card.dataset.turnos || "[]");
    } catch (error) {
      keys = [];
    }
    if (keys.length) {
      askKeys(next, keys, { respuesta: msg.text, parent_id: msg.id || null });
    }
  }
}

function onAnswers(msg) {
  const box = el("answers");
  box.innerHTML = "";
  pendingCard = null;
  // Llegan de la más nueva a la más vieja. Se recorren al revés para que cada
  // ampliación encuentre a su padre ya puesto y se acomode debajo.
  for (const row of [...msg.rows].reverse()) {
    fillActions(answerCard(row));
  }
  for (const card of box.children) card.classList.remove("fresh");
  el("answers-empty").classList.toggle("hidden", box.children.length > 0);
  countAnswers();
}

function countAnswers() {
  const n = el("answers").children.length;
  el("answers-count").textContent = n ? `(${n})` : "";
}

// ---------------------------------------------------------------- búsqueda

let searchTimer = null;
el("q").oninput = () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    const q = el("q").value.trim();
    if (!q) return onResults([]);
    send({
      type: "search",
      q,
      session_id: el("only-session").checked ? viewing || sessionId : null,
    });
  }, 250);
};

function onResults(rows) {
  const box = el("results");
  box.innerHTML = "";
  if (!rows.length) return;
  for (const row of rows) {
    const hit = document.createElement("div");
    hit.className = "hit";
    hit.innerHTML = `<div>${escapeHtml(row.text)}</div>
      <small>${SPEAKER[row.track] || row.track} · ${row.t_start.toFixed(1)}s</small>`;
    hit.onclick = () => send({ type: "transcript", session_id: row.session_id });
    box.appendChild(hit);
  }
}

// ---------------------------------------------------------------- sesiones

function sessionLabel(row) {
  // Las sesiones viejas guardaban `time.monotonic()`, que son segundos desde que
  // arrancó la máquina y no una fecha. Se reconocen porque son números chicos.
  if (!row.started_at || row.started_at < 1e9) return row.id;
  const date = new Date(row.started_at * 1000);
  const today = new Date();
  const sameDay = date.toDateString() === today.toDateString();
  const time = date.toLocaleTimeString("es", { hour: "2-digit", minute: "2-digit" });
  if (sameDay) return `Hoy ${time}`;
  const day = date.toLocaleDateString("es", { day: "numeric", month: "short" });
  return `${day} ${time}`;
}

function onSessions(rows) {
  const box = el("sessions");
  box.innerHTML = "";
  for (const row of rows) {
    const item = document.createElement("div");
    item.className = "session";
    const when = row.ended_at ? "" : " · abierta";
    item.innerHTML = `<div>${sessionLabel(row)}</div>
      <small>${row.segmentos} segmento${row.segmentos === 1 ? "" : "s"}${when}</small>`;
    item.title = row.id;
    item.onclick = () => send({ type: "transcript", session_id: row.id });
    box.appendChild(item);
  }
}

// ---------------------------------------------------------------- captura

el("toggle").onclick = () => {
  if (running) return send({ type: "stop" });
  clearStream();
  el("status").textContent = "arrancando…";
  send({
    type: "start",
    lang_mic: el("lang-mic").value,
    lang_sys: el("lang-sys").value,
    traducir: el("traducir").checked ? "es" : null,
    // "tap" no es un dispositivo: lo resuelve make_source() del lado de Python.
    sys_device: el("nativo").checked ? "tap" : null,
  });
};

function onState(msg) {
  running = msg.running;
  sessionId = msg.session_id;
  if (running) viewing = null;
  el("toggle").textContent = running ? "Parar" : "Empezar";
  el("toggle").classList.toggle("rec", running);
  el("dot").classList.toggle("rec", running);
  el("status").textContent = running ? `grabando · ${msg.backend || ""}` : "listo";
  if (!running) send({ type: "sessions" });
}

// ---------------------------------------------------------------- varios

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

function toast(message) {
  const node = document.createElement("div");
  node.className = "toast";
  node.textContent = message;
  document.body.appendChild(node);
  setTimeout(() => node.remove(), 6000);
}

document.addEventListener("click", (event) => {
  const menu = el("settings");
  if (menu.open && !menu.contains(event.target)) menu.open = false;
});

// ---------------------------------------------------------------- ancho del panel

const ASIDE_DEFAULT = 330;
const ASIDE_MIN = 240;
const ASIDE_MAX_RATIO = 0.62; // el panel nunca se come la transcripción entera

function currentAside() {
  const raw = getComputedStyle(document.documentElement).getPropertyValue("--aside");
  return parseInt(raw, 10) || ASIDE_DEFAULT;
}

/** Aplica el ancho, acotado a la ventana. No guarda: eso lo decide quien llama. */
function applyAside(px) {
  // Durante un reflow la ventana puede reportar ancho 0. Acotar contra eso daría el
  // mínimo y, si además se guardara, se perdería el ancho que eligió el usuario.
  const room = window.innerWidth || 0;
  const max = room > 400 ? Math.round(room * ASIDE_MAX_RATIO) : Infinity;
  const width = Math.round(Math.min(Math.max(px, ASIDE_MIN), max));
  document.documentElement.style.setProperty("--aside", `${width}px`);
  return width;
}

/** Aplica y recuerda. Sólo para cambios que pidió el usuario. */
function setAside(px) {
  const width = applyAside(px);
  try {
    localStorage.setItem("aside", String(width));
  } catch (error) {
    // Modo privado o storage lleno: el ancho vale para esta sesión y nada más.
  }
  return width;
}

(function setupInstrucciones() {
  const box = el("instrucciones");
  const card = el("instrucciones-card");
  const estado = el("instrucciones-estado");

  const refresh = () => {
    const chars = box.value.trim().length;
    estado.textContent = chars ? `(${chars} caracteres)` : "(vacío)";
  };

  box.value = localStorage.getItem("instrucciones") || "";
  // Abierto si ya hay algo escrito: si está vacío, no vale la pena que ocupe lugar.
  card.open = box.value.trim().length > 0;
  refresh();

  box.addEventListener("input", () => {
    refresh();
    try {
      localStorage.setItem("instrucciones", box.value);
    } catch (error) {
      // Sin storage las instrucciones duran lo que dure la ventana.
    }
  });
})();

(function setupAutoAmpliar() {
  const box = el("auto-ampliar");
  const stored = localStorage.getItem("auto-ampliar");
  if (stored !== null) box.checked = stored === "1";
  box.addEventListener("change", () => {
    try {
      localStorage.setItem("auto-ampliar", box.checked ? "1" : "0");
    } catch (error) {
      // Sin storage la preferencia dura lo que dure la ventana.
    }
  });
})();

(function setupSplitter() {
  const splitter = el("splitter");
  const stored = Number(localStorage.getItem("aside"));
  applyAside(stored > 0 ? stored : ASIDE_DEFAULT);

  const widthFrom = (clientX) => window.innerWidth - clientX - 3;

  splitter.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    // Capturar el puntero: sin esto, arrastrar rápido lo pierde apenas sale del
    // separador, que tiene 7 px de ancho.
    splitter.setPointerCapture(event.pointerId);
    splitter.classList.add("dragging");
    document.body.classList.add("resizing");

    const move = (moveEvent) => setAside(widthFrom(moveEvent.clientX));
    const up = () => {
      splitter.classList.remove("dragging");
      document.body.classList.remove("resizing");
      splitter.removeEventListener("pointermove", move);
      splitter.removeEventListener("pointerup", up);
      splitter.removeEventListener("pointercancel", up);
    };
    splitter.addEventListener("pointermove", move);
    splitter.addEventListener("pointerup", up);
    splitter.addEventListener("pointercancel", up);
  });

  splitter.addEventListener("dblclick", () => setAside(ASIDE_DEFAULT));

  splitter.addEventListener("keydown", (event) => {
    const step = event.shiftKey ? 40 : 12;
    if (event.key === "ArrowLeft") setAside(currentAside() + step);
    else if (event.key === "ArrowRight") setAside(currentAside() - step);
    else return;
    event.preventDefault();
  });

  // Achicar la ventana acota el panel, pero no cambia el ancho elegido: al volver a
  // agrandarla se recupera.
  window.addEventListener("resize", () => {
    const stored = Number(localStorage.getItem("aside")) || currentAside();
    applyAside(stored);
  });
})();

connect();
