// Одно WebSocket-соединение на всё: и меню, и лобби, и заезд.
// Отдельного HTTP-API нет — так проще и нечему рассинхронизироваться.

const PING_EVERY = 2000;
const RTT_SAMPLES = 12;

export class Net {
  constructor(onMessage, onStatus) {
    this.onMessage = onMessage;
    this.onStatus = onStatus;
    this.ws = null;
    this.seq = 0;
    this.rtts = [];
    this.rtt = 0;
    this.clockOffset = 0;
    this.tries = 0;
    this.wantOpen = true;
    this.lastError = '';
    this._pingTimer = null;
    this._retryTimer = null;
  }

  get open() {
    return this.ws && this.ws.readyState === WebSocket.OPEN;
  }

  url() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${proto}//${location.host}/ws`;
  }

  connect() {
    this.wantOpen = true;
    clearTimeout(this._retryTimer);
    if (this.ws && (this.ws.readyState === WebSocket.CONNECTING ||
                    this.ws.readyState === WebSocket.OPEN)) return;

    this.onStatus(this.tries === 0 ? 'connecting' : 'reconnecting', this.lastError);
    let ws;
    try {
      ws = new WebSocket(this.url());
    } catch (e) {
      this.lastError = String(e);
      return this._retry();
    }
    this.ws = ws;

    ws.onopen = () => {
      this.tries = 0;
      this.onStatus('open', '');
      clearInterval(this._pingTimer);
      this._pingTimer = setInterval(() => this.ping(), PING_EVERY);
      this.ping();
    };
    ws.onclose = (ev) => {
      clearInterval(this._pingTimer);
      if (this.ws === ws) this.ws = null;
      // 1006 без причины — почти всегда «сервер остановили» или потеря сети
      this.lastError = ev.reason || (ev.code === 1006
        ? 'соединение оборвалось (сервер остановлен или пропала сеть)'
        : `код ${ev.code}`);
      this.onStatus('closed', this.lastError);
      if (this.wantOpen) this._retry();
    };
    ws.onerror = () => { this.lastError = 'ошибка соединения'; };
    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.t === 'pong') return this._pong(msg);
      this.onMessage(msg);
    };
  }

  _retry() {
    this.tries++;
    // Растущая пауза, но не больше 4 с: в локалке сервер обычно возвращается
    // быстро, и ждать 30 секунд бессмысленно.
    const wait = Math.min(4000, 300 * Math.pow(1.7, Math.min(this.tries, 6)));
    clearTimeout(this._retryTimer);
    this._retryTimer = setTimeout(() => this.connect(), wait);
  }

  close() {
    this.wantOpen = false;
    clearInterval(this._pingTimer);
    clearTimeout(this._retryTimer);
    if (this.ws) this.ws.close();
  }

  send(obj) {
    if (!this.open) return false;
    try { this.ws.send(JSON.stringify(obj)); return true; }
    catch { return false; }
  }

  ping() {
    if (this.open) this.send({ t: 'ping', c: performance.now() });
  }

  _pong(msg) {
    const rtt = performance.now() - msg.c;
    this.rtts.push(rtt);
    if (this.rtts.length > RTT_SAMPLES) this.rtts.shift();
    // Медиана, а не среднее: одиночный подвисший ответ не должен
    // раздувать оценку задержки.
    const sorted = [...this.rtts].sort((a, b) => a - b);
    this.rtt = sorted[sorted.length >> 1];
    this.clockOffset = msg.s - (msg.c + rtt / 2);
  }

  sendInput(tick, mask) {
    this.seq++;
    return this.send({ t: 'input', q: this.seq, k: tick, m: mask });
  }
}
