/* WebSocket-клиент: реконнект, пинг, подписка на сообщения. */
class Net {
  constructor() {
    this.ws = null; this.handlers = {}; this.pid = null;
    this.ping = 0; this.open = false; this._retry = 0; this._q = [];
  }
  on(t, fn) { (this.handlers[t] = this.handlers[t] || []).push(fn); return this; }
  emit(t, m) { (this.handlers[t] || []).forEach(f => f(m)); }

  connect() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    this.ws = new WebSocket(`${proto}://${location.host}/ws`);
    this.ws.onopen = () => {
      this.open = true; this._retry = 0;
      this._q.splice(0).forEach(m => this.send(m));
      this.emit('_open');
      clearInterval(this._pt);
      this._pt = setInterval(() => this.send({ t: 'ping', ts: performance.now() }), 2000);
    };
    this.ws.onclose = () => {
      this.open = false; clearInterval(this._pt); this.emit('_close');
      this._retry++;
      setTimeout(() => this.connect(), Math.min(4000, 400 * this._retry));
    };
    this.ws.onmessage = ev => {
      const m = JSON.parse(ev.data);
      if (m.t === 'pong') { this.ping = Math.round(performance.now() - m.ts); return; }
      if (m.t === 'welcome') this.pid = m.pid;
      this.emit(m.t, m);
    };
  }
  send(m) {
    if (this.ws && this.ws.readyState === 1) this.ws.send(JSON.stringify(m));
    else if (m.t !== 'i') this._q.push(m);
  }
}
