// Cara de granola-local. Toda la lógica vive en el sidecar de Python; esto pinta lo
// que llega por el WebSocket y manda de vuelta lo que el usuario pide.

const PORT = new URLSearchParams(location.search).get("port") || "8765";
const SPEAKER = { mic: "Yo", sys: "Ellos" };

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
    case "sessions": return onSessions(msg.rows);
    case "transcript": return onTranscript(msg);
    case "search_result": return onResults(msg.rows);
    case "answer": return onAnswer(msg);
    case "error": {
      const answer = el("answer");
      if (answer.classList.contains("pending")) {
        answer.classList.remove("pending");
        answer.textContent = "";
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

  for (const template of templates) {
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
  for (const t of msg.templates) {
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

function askKeys(template, keys) {
  if (!keys.length) return toast("Elegí al menos una frase.");
  const answer = el("answer");
  answer.classList.add("pending");
  answer.textContent = "pensando…";
  send({
    type: "ask",
    template,
    cli: el("cli").value || null,
    idioma: el("idioma").value,
    session_id: viewing || sessionId,
    keys,
  });
}

function onAnswer(msg) {
  const answer = el("answer");
  answer.classList.remove("pending");
  answer.innerHTML = "";
  const meta = document.createElement("div");
  meta.className = "meta";
  const who = msg.local
    ? `${msg.cli} · local`
    : `<span class="remote">${msg.cli} · remota — el texto salió de tu máquina</span>`;
  meta.innerHTML = `${who} · en ${msg.idioma} · ${msg.elapsed.toFixed(1)}s`;
  const body = document.createElement("div");
  body.textContent = msg.text;
  answer.append(meta, body);
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
  return sameDay ? `Hoy ${time}` : `${date.toLocaleDateString("es", { day: "numeric", month: "short" })} ${time}`;
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

connect();
