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
    case "error": return toast(msg.message);
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
  node.innerHTML = `<div class="who"><b>${SPEAKER[track] || track}</b><span class="t"></span></div>
                    <div class="text"></div><div class="trad"></div>`;
  node.onclick = () => toggleSelect(node, track, index);
  stream.appendChild(node);
  el("empty").classList.add("hidden");
  return node;
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
  renderSelection();
  el("empty").classList.toggle("hidden", false);
}

// ---------------------------------------------------------------- selección

function toggleSelect(node, track, index) {
  const id = key(track, index);
  if (selected.has(id)) selected.delete(id);
  else selected.set(id, { track, turn_index: index });
  node.classList.toggle("sel", selected.has(id));
  renderSelection();
}

function renderSelection() {
  const n = selected.size;
  el("selcount").textContent = n
    ? `${n} fragmento${n > 1 ? "s" : ""} elegido${n > 1 ? "s" : ""}.`
    : "Ninguno. Hacé clic en las burbujas para elegir.";
}

el("clear").onclick = () => {
  for (const id of selected.keys()) document.getElementById(id)?.classList.remove("sel");
  selected.clear();
  renderSelection();
};

// ---------------------------------------------------------------- prompts

function onPrompts(msg) {
  const box = el("prompts");
  box.innerHTML = "";
  for (const t of msg.templates) {
    const button = document.createElement("button");
    button.textContent = t.nombre;
    button.title = t.descripcion || "";
    button.onclick = () => ask(t.id);
    box.appendChild(button);
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
  if (!selected.size) return toast("Elegí al menos un fragmento.");
  const answer = el("answer");
  answer.classList.remove("hidden");
  answer.textContent = "pensando…";
  send({
    type: "ask",
    template,
    cli: el("cli").value || null,
    session_id: viewing || sessionId,
    keys: [...selected.values()],
  });
}

function onAnswer(msg) {
  const answer = el("answer");
  answer.innerHTML = "";
  const meta = document.createElement("div");
  meta.className = "meta";
  meta.innerHTML = msg.local
    ? `${msg.cli} · local · ${msg.elapsed.toFixed(1)}s`
    : `<span class="remote">${msg.cli} · remota — el texto salió de tu máquina</span> · ${msg.elapsed.toFixed(1)}s`;
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

function onSessions(rows) {
  const box = el("sessions");
  box.innerHTML = "";
  for (const row of rows) {
    const item = document.createElement("div");
    item.className = "session";
    const when = row.ended_at ? "cerrada" : "abierta";
    item.innerHTML = `<div>${row.id}</div><small>${row.segmentos} segmentos · ${when}</small>`;
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

connect();
