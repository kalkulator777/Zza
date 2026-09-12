import { Board } from "./draw.js";
import { connect } from "./net.js";

const $ = (id) => document.getElementById(id);

const code = location.pathname.split("/").filter(Boolean).pop().toUpperCase();
const params = new URLSearchParams(location.search);
const nick = (params.get("nick") || localStorage.getItem("nick") || "Игрок").slice(0, 16);
localStorage.setItem("nick", nick);
const TOKEN_KEY = "token:" + code;

const PALETTE = [
  "#000000", "#555555", "#aaaaaa", "#ffffff", "#7b3f00", "#ff5c5c",
  "#ff9f43", "#ffe14d", "#6ee07a", "#2ecc71", "#4dd2c0", "#4db8ff",
  "#2f6fed", "#8f7bff", "#d07bff", "#ff6bcb", "#ffb6c1", "#c1855c",
  "#3a2d1f", "#0b6e4f", "#123a8f", "#5c2d91",
];
const SIZES = [4, 9, 18, 32];
const PLANNED = ["Анимация", "Дополнение", "Изысканный труп", "Сотрудничество"];
const SETTING_KEYS = ["rounds", "steps", "draw_time", "write_time", "hints", "difficulty", "custom_words"];

let room = null;
let game = null;
let myToken = localStorage.getItem(TOKEN_KEY) || null;
let deadlineAt = 0;

$("code").textContent = code;

const board = new Board($("board"), {
  onOps: (ops) => net.send({ t: "draw", ops }),
});
// второй холст — только показать чужой рисунок: источник хода или кадр показа
const sourceBoard = new Board($("source"));

const net = connect({
  code,
  nick,
  getToken: () => myToken,
  onMessage: handle,
  onStatus: (status) => {
    $("status").textContent = status === "online" ? "" : "нет связи, переподключаюсь…";
  },
});

// ---------- входящие сообщения ----------

function handle(message) {
  switch (message.t) {
    case "joined":
      myToken = message.token;
      localStorage.setItem(TOKEN_KEY, myToken);
      break;
    case "room":
      room = message;
      renderViews();
      break;
    case "game":
      game = message;
      if (typeof message.remaining === "number") deadlineAt = Date.now() + message.remaining;
      if (message.canvas) board.setOps(message.canvas);
      renderGame();
      break;
    case "draw":
      if (!game || !game.youAreArtist) board.applyOps(message.ops || []);
      break;
    case "canvas":
      board.setOps(message.ops || []);
      break;
    case "tick":
      deadlineAt = Date.now() + message.remaining;
      break;
    case "chat":
      pushChat(message);
      break;
    case "results":
      renderResults(message);
      break;
    case "toast":
      toast(message.text);
      break;
    case "kicked":
      net.stop();
      alert(message.text);
      location.href = "/";
      break;
  }
}

// ---------- виды ----------

function renderViews() {
  const state = room ? room.state : "lobby";
  $("view-lobby").classList.toggle("hidden", state !== "lobby");
  $("view-game").classList.toggle("hidden", state !== "playing");
  $("view-results").classList.toggle("hidden", state !== "results");
  if (state === "lobby") renderLobby();
  if (state === "playing") renderGame();
}

function isHost() {
  return room && room.host === myToken;
}

function playerCard(player, extra = {}) {
  const card = document.createElement("div");
  card.className = "player";
  if (extra.drawing) card.classList.add("drawing");
  if (extra.guessed) card.classList.add("guessed");
  if (!player.online) card.classList.add("offline");

  const avatar = document.createElement("div");
  avatar.className = "avatar";
  avatar.style.background = player.color;

  const name = document.createElement("div");
  name.className = "nick";
  name.textContent = player.nick;
  if (player.token === myToken) card.classList.add("me");

  card.append(avatar, name);

  if (room && room.host === player.token) {
    const crown = document.createElement("span");
    crown.className = "crown";
    crown.textContent = "★";
    crown.title = "Хозяин лобби";
    card.append(crown);
  }
  if (extra.score) {
    const score = document.createElement("div");
    score.className = "score";
    score.textContent = player.score;
    card.append(score);
  }
  if (extra.kick && isHost() && player.token !== myToken) {
    const kick = document.createElement("button");
    kick.className = "btn small ghost";
    kick.textContent = "×";
    kick.title = "Выгнать";
    kick.addEventListener("click", () => net.send({ t: "kick", token: player.token }));
    card.append(kick);
  }
  return card;
}

function renderLobby() {
  const box = $("lobby-players");
  box.textContent = "";
  for (const player of room.players) box.append(playerCard(player, { kick: true }));

  const cards = $("mode-cards");
  cards.textContent = "";
  for (const [key, info] of Object.entries(room.modes)) {
    const card = document.createElement("div");
    card.className = "mode-card" + (room.settings.mode === key ? " active" : "");
    const head = document.createElement("h3");
    head.textContent = info.title;
    const text = document.createElement("p");
    text.textContent = info.about;
    card.append(head, text);
    card.addEventListener("click", () => {
      if (isHost()) net.send({ t: "settings", patch: { mode: key } });
    });
    cards.append(card);
  }
  for (const title of PLANNED) {
    const card = document.createElement("div");
    card.className = "mode-card soon";
    const head = document.createElement("h3");
    head.textContent = title;
    const text = document.createElement("p");
    text.textContent = "скоро";
    card.append(head, text);
    cards.append(card);
  }

  for (const key of SETTING_KEYS) {
    const input = $("s-" + key);
    if (!input) continue;
    if (document.activeElement !== input) input.value = room.settings[key];
    input.disabled = !isHost();
  }
  const isAlbum = room.settings.mode !== "guess";
  document.querySelectorAll(".only-guess").forEach((el) => el.classList.toggle("hidden", isAlbum));
  document.querySelectorAll(".only-album").forEach((el) => el.classList.toggle("hidden", !isAlbum));

  const enoughPlayers = room.players.length >= 2;
  $("start").disabled = !isHost() || !enoughPlayers;
  $("start").textContent = enoughPlayers ? "Начать" : "Нужно хотя бы двое";
  $("host-note").textContent = isHost()
    ? "Ты хозяин лобби: выбираешь режим и запускаешь игру"
    : "Настройки меняет хозяин лобби";
  $("invite").textContent = room.invite ? "Адрес для соседей: " + room.invite : "";
}

function renderGame() {
  if (!game) return;
  if (game.family === "album") renderAlbum();
  else renderGuess();
}

function renderGuess() {
  const artistNow = game.youAreArtist && game.phase === "drawing";
  $("album-bar").classList.add("hidden");
  $("source-box").classList.add("hidden");
  $("source-text").classList.add("hidden");
  $("canvas-box").classList.remove("hidden");
  $("chat-input").disabled = false;
  $("word").style.letterSpacing = "";
  $("word").style.fontSize = "";

  const box = $("game-players");
  box.textContent = "";
  for (const player of (room ? room.players : [])) {
    box.append(playerCard(player, {
      score: true,
      drawing: player.token === game.artist,
      guessed: (game.guessed || []).includes(player.token),
    }));
  }

  $("round").textContent = game.round ? `Раунд ${game.round}/${game.rounds}` : "";
  $("word").textContent = game.word || game.mask || "";

  $("tools").classList.toggle("hidden", !artistNow);
  $("canvas-box").classList.toggle("watching", !artistNow);
  board.enabled = artistNow;

  const input = $("chat-input");
  input.placeholder = game.youAreArtist ? "Не подсказывай словами!" : "Пиши догадку…";

  renderOverlay();
}

function renderAlbum() {
  const g = game;
  $("overlay").classList.add("hidden");
  $("album-bar").classList.remove("hidden");
  const chatInput = $("chat-input");
  chatInput.disabled = g.phase === "task";
  chatInput.placeholder = chatInput.disabled ? "Чат откроется на показе" : "Пиши сюда";
  $("next").classList.toggle("hidden", !(g.phase === "present" && g.isHost));

  const box = $("game-players");
  box.textContent = "";
  const submitted = g.submitted || [];
  for (const player of (room ? room.players : [])) {
    box.append(playerCard(player, { guessed: submitted.includes(player.token) }));
  }

  if (g.phase === "task") renderAlbumTask(g);
  else renderAlbumShow(g);
}

function renderAlbumTask(g) {
  const isText = g.task === "text";
  const source = g.source;

  $("round").textContent = `Ход ${g.round}/${g.rounds}`;
  $("word").textContent = g.hint || "";
  $("word").style.letterSpacing = "0";
  $("word").style.fontSize = "20px";

  const sourceIsText = !!source && source.type === "text";
  $("source-text").classList.toggle("hidden", !sourceIsText);
  if (sourceIsText) $("source-text").textContent = source.text;

  const sourceIsDraw = !!source && source.type === "draw";
  $("source-box").classList.toggle("hidden", !sourceIsDraw);
  if (sourceIsDraw) sourceBoard.setOps(source.ops || []);

  $("canvas-box").classList.toggle("hidden", isText);
  $("canvas-box").classList.toggle("watching", g.done);
  $("tools").classList.toggle("hidden", isText || g.done || !g.playing);
  board.enabled = !isText && !g.done && g.playing;

  const textField = $("album-text");
  textField.classList.toggle("hidden", !isText || !g.playing);
  textField.disabled = g.done;
  if (!isText) textField.value = "";

  $("submit").classList.toggle("hidden", g.done || !g.playing);
  $("album-note").textContent = g.done
    ? "Ждём: " + (g.waiting || []).join(", ")
    : (g.playing ? "" : "Ты смотришь со стороны");
}

function renderAlbumShow(g) {
  const step = g.step;
  $("round").textContent = `Альбом ${g.albumIndex + 1}/${g.albumsTotal} · шаг ${g.stepIndex + 1}/${g.stepsTotal}`;
  $("word").textContent = `Начал ${g.ownerNick}`;
  $("word").style.letterSpacing = "0";
  $("word").style.fontSize = "20px";

  $("tools").classList.add("hidden");
  $("source-box").classList.add("hidden");
  $("album-text").classList.add("hidden");
  $("submit").classList.add("hidden");
  board.enabled = false;

  if (!step) return;
  const isText = step.type === "text";
  $("source-text").classList.toggle("hidden", !isText);
  if (isText) $("source-text").textContent = step.text;
  $("canvas-box").classList.toggle("hidden", isText);
  $("canvas-box").classList.add("watching");
  if (!isText) board.setOps(step.ops || []);
  $("album-note").textContent = (isText ? "написал " : "нарисовал ") + (step.authorNick || "");
}

function renderOverlay() {
  const overlay = $("overlay");
  overlay.textContent = "";
  if (!game || game.phase === "drawing") {
    overlay.classList.add("hidden");
    return;
  }
  overlay.classList.remove("hidden");

  const title = document.createElement("h2");
  if (game.phase === "choosing") {
    if (game.youAreArtist) {
      title.textContent = "Выбирай слово";
      overlay.append(title);
      const choices = document.createElement("div");
      choices.className = "choices";
      (game.choices || []).forEach((choiceWord, index) => {
        const button = document.createElement("button");
        button.className = "btn primary";
        button.textContent = choiceWord;
        button.addEventListener("click", () => net.send({ t: "pick", index }));
        choices.append(button);
      });
      overlay.append(choices);
    } else {
      title.textContent = `${game.artistNick} выбирает слово…`;
      overlay.append(title);
    }
  } else if (game.phase === "reveal") {
    title.textContent = "Слово: " + (game.word || "");
    overlay.append(title);
    const deltas = document.createElement("div");
    deltas.className = "deltas";
    for (const [token, points] of Object.entries(game.deltas || {})) {
      const player = (room ? room.players : []).find((p) => p.token === token);
      if (!player) continue;
      const line = document.createElement("div");
      line.textContent = `${player.nick}: +${points}`;
      deltas.append(line);
    }
    overlay.append(deltas);
  }
}

let gallery = { albums: [], index: 0 };

function renderResults(message) {
  if (message.albums && message.albums.length) {
    gallery = { albums: message.albums, index: 0 };
    renderGallery();
    $("again").disabled = !isHost();
    return;
  }
  const box = $("results");
  box.textContent = "";
  (message.table || []).forEach((player, index) => {
    const row = document.createElement("div");
    row.className = "results-row" + (index === 0 ? " first" : "");
    const place = document.createElement("div");
    place.className = "place";
    place.textContent = index + 1;
    const avatar = document.createElement("div");
    avatar.className = "avatar";
    avatar.style.background = player.color;
    const name = document.createElement("div");
    name.className = "nick";
    name.style.flex = "1";
    name.textContent = player.nick;
    const score = document.createElement("div");
    score.className = "score";
    score.textContent = player.score;
    row.append(place, avatar, name, score);
    box.append(row);
  });
  $("again").disabled = !isHost();
}

function renderGallery() {
  const box = $("results");
  box.textContent = "";
  const album = gallery.albums[gallery.index];
  if (!album) return;

  const head = document.createElement("div");
  head.className = "gallery-head";
  const prev = document.createElement("button");
  prev.className = "btn small";
  prev.textContent = "◀";
  prev.disabled = gallery.index === 0;
  prev.addEventListener("click", () => { gallery.index -= 1; renderGallery(); });
  const title = document.createElement("div");
  title.innerHTML = "";
  title.textContent = `Альбом ${album.ownerNick} · ${gallery.index + 1}/${gallery.albums.length}`;
  const next = document.createElement("button");
  next.className = "btn small";
  next.textContent = "▶";
  next.disabled = gallery.index >= gallery.albums.length - 1;
  next.addEventListener("click", () => { gallery.index += 1; renderGallery(); });
  head.append(prev, title, next);

  const steps = document.createElement("div");
  steps.className = "album-steps";
  for (const step of album.steps) {
    const card = document.createElement("div");
    card.className = "album-step";
    const author = document.createElement("div");
    author.className = "author";
    author.textContent = (step.type === "text" ? "написал " : "нарисовал ") + (step.authorNick || "");
    card.append(author);
    if (step.type === "text") {
      const said = document.createElement("div");
      said.className = "said";
      said.textContent = step.text;
      card.append(said);
    } else {
      const canvas = document.createElement("canvas");
      card.append(canvas);
      new Board(canvas).setOps(step.ops || []);
    }
    steps.append(card);
  }
  box.append(head, steps);
}

// ---------- чат ----------

function pushChat(message) {
  const log = $("chat");
  const line = document.createElement("div");
  line.className = "msg " + (message.kind || "chat");
  if (message.from) {
    const who = document.createElement("b");
    who.textContent = message.from + ": ";
    if (message.color) who.style.color = message.color;
    line.append(who);
  }
  line.append(document.createTextNode(message.text));
  log.append(line);
  while (log.children.length > 120) log.firstChild.remove();
  log.scrollTop = log.scrollHeight;
}

$("chat-input").addEventListener("keydown", (event) => {
  if (event.key !== "Enter") return;
  const text = event.target.value.trim();
  if (!text) return;
  net.send({ t: "chat", text });
  event.target.value = "";
});

// ---------- инструменты ----------

const swatches = $("swatches");
PALETTE.forEach((color) => {
  const dot = document.createElement("div");
  dot.className = "swatch" + (color === board.color ? " active" : "");
  dot.style.background = color;
  dot.addEventListener("click", () => {
    board.color = color;
    if (board.tool === "eraser") selectTool("pen");
    swatches.querySelectorAll(".swatch").forEach((s) => s.classList.remove("active"));
    dot.classList.add("active");
  });
  swatches.append(dot);
});

const sizes = $("sizes");
SIZES.forEach((size) => {
  const dot = document.createElement("div");
  dot.className = "size-dot" + (size === board.size ? " active" : "");
  dot.style.width = dot.style.height = Math.max(10, size) + "px";
  dot.addEventListener("click", () => {
    board.size = size;
    sizes.querySelectorAll(".size-dot").forEach((s) => s.classList.remove("active"));
    dot.classList.add("active");
  });
  sizes.append(dot);
});

function selectTool(tool) {
  board.tool = tool;
  document.querySelectorAll(".tool").forEach((button) => {
    button.classList.toggle("active", button.dataset.tool === tool);
  });
}
document.querySelectorAll(".tool").forEach((button) => {
  button.addEventListener("click", () => selectTool(button.dataset.tool));
});

$("undo").addEventListener("click", () => {
  board.undo();
  net.send({ t: "undo" });
});
$("clear").addEventListener("click", () => {
  board.clear();
  net.send({ t: "clear" });
});

// ---------- прочее ----------

for (const key of SETTING_KEYS) {
  const input = $("s-" + key);
  if (!input) continue;
  input.addEventListener("change", () => {
    net.send({ t: "settings", patch: { [key]: input.value } });
  });
}

$("submit").addEventListener("click", () => {
  net.send({ t: "submit", text: $("album-text").value.trim() });
});
$("album-text").addEventListener("keydown", (event) => {
  if (event.key === "Enter") $("submit").click();
});
$("next").addEventListener("click", () => net.send({ t: "next" }));

$("start").addEventListener("click", () => net.send({ t: "start" }));
$("again").addEventListener("click", () => net.send({ t: "again" }));
$("leave").addEventListener("click", () => {
  net.stop();
  location.href = "/";
});

let toastTimer = null;
function toast(text) {
  let element = document.querySelector(".toast");
  if (!element) {
    element = document.createElement("div");
    element.className = "toast";
    document.body.append(element);
  }
  element.textContent = text;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => element.remove(), 2600);
}

setInterval(() => {
  if (!room || room.state !== "playing" || !game) return;
  const left = Math.max(0, Math.ceil((deadlineAt - Date.now()) / 1000));
  $("timer").textContent = left;
  $("timer").classList.toggle("hot", left <= 10 && game.phase === "drawing");
}, 200);
