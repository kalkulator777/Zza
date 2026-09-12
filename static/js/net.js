// Соединение с сервером комнаты: переподключается само, чтобы F5 или моргнувшая
// сеть не выкидывали игрока из партии.
export function connect({ code, nick, getToken, onMessage, onStatus }) {
  let socket = null;
  let attempts = 0;
  let stopped = false;

  function open() {
    if (stopped) return;
    socket = new WebSocket(`ws://${location.host}/ws`);
    socket.addEventListener("open", () => {
      attempts = 0;
      onStatus("online");
      send({ t: "join", code, nick, token: getToken() });
    });
    socket.addEventListener("message", (event) => {
      let message;
      try { message = JSON.parse(event.data); } catch (e) { return; }
      onMessage(message);
    });
    socket.addEventListener("close", () => {
      if (stopped) return;
      onStatus("offline");
      attempts += 1;
      setTimeout(open, Math.min(5000, 400 * attempts));
    });
  }

  function send(message) {
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify(message));
    }
  }

  open();
  return {
    send,
    stop() { stopped = true; if (socket) socket.close(); },
  };
}
