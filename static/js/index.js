const $ = (id) => document.getElementById(id);

const nickInput = $("nick");
nickInput.value = localStorage.getItem("nick") || "";
nickInput.focus();
nickInput.addEventListener("input", () => localStorage.setItem("nick", nickInput.value.trim()));

const params = new URLSearchParams(location.search);
if (params.get("err")) {
  $("error").textContent = params.get("err");
  $("error").classList.remove("hidden");
}
if (params.get("nick") && !nickInput.value) {
  nickInput.value = params.get("nick");
  localStorage.setItem("nick", nickInput.value);
}

function nick() {
  const value = nickInput.value.trim();
  if (!value) {
    nickInput.focus();
    nickInput.classList.add("error");
    setTimeout(() => nickInput.classList.remove("error"), 600);
    return null;
  }
  localStorage.setItem("nick", value);
  return value;
}

$("create").addEventListener("click", async () => {
  const name = nick();
  if (!name) return;
  $("create").disabled = true;
  try {
    const response = await fetch("/api/create", { method: "POST" });
    const data = await response.json();
    location.href = `/room/${data.code}?nick=${encodeURIComponent(name)}`;
  } catch (err) {
    $("error").textContent = "Не получилось создать лобби: " + err;
    $("error").classList.remove("hidden");
    $("create").disabled = false;
  }
});

function renderLobbies(rooms) {
  const box = $("lobbies");
  box.textContent = "";
  if (!rooms.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "Пока никто не создал лобби. Создай первым!";
    box.append(empty);
    return;
  }
  for (const room of rooms) {
    const item = document.createElement("div");
    item.className = "lobby-item";
    const who = document.createElement("div");
    who.className = "who";
    const title = document.createElement("b");
    title.textContent = room.owner ? `Лобби ${room.owner}` : `Лобби ${room.code}`;
    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = `${room.host} · код ${room.code} · ${room.mode} · игроков ${room.players}/${room.max}`;
    who.append(title, meta);
    const join = document.createElement("button");
    join.className = "btn small primary";
    join.textContent = "Зайти";
    item.append(who, join);
    item.addEventListener("click", () => {
      const name = nick();
      if (!name) return;
      location.href = `http://${room.ip}:${room.port}/room/${room.code}?nick=${encodeURIComponent(name)}`;
    });
    box.append(item);
  }
}

async function scan() {
  $("refresh").disabled = true;
  try {
    const response = await fetch("/api/scan");
    const data = await response.json();
    renderLobbies(data.rooms || []);
  } catch (err) {
    renderLobbies([]);
  } finally {
    $("refresh").disabled = false;
  }
}

$("refresh").addEventListener("click", scan);
scan();

$("go").addEventListener("click", () => {
  const name = nick();
  if (!name) return;
  let target = $("manual").value.trim().replace(/^https?:\/\//, "").replace(/\/+$/, "");
  if (!target) return;
  if (!target.includes(":")) target += ":8770";
  location.href = `http://${target}/?nick=${encodeURIComponent(name)}`;
});
$("manual").addEventListener("keydown", (e) => { if (e.key === "Enter") $("go").click(); });
