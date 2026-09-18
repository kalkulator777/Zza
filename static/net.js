// -*- coding: utf-8 -*-
// WebSocket и время (DESIGN.md 5, 6).
//
// Здесь и только здесь знают форму сообщений на проводе. Мир (state.js) и
// рендер (render2d.js) про сеть не знают вовсе — это требование 7.1
// («ни один бэкенд не читает сеть»), и для мира оно так же полезно.

import { rleDecode } from './state.js';

export const PROTO = 1;
export const INPUT_HZ = 30;          // 5.1: ровно как тик, чаще не нужно
const PING_MS = 1000;
const RECONNECT_MS = 1500;

function wsUrl() {
  const scheme = location.protocol === 'https:' ? 'wss://' : 'ws://';
  return scheme + location.host + '/ws';
}

export class Net {
  /**
   * @param {object} h — обработчики: onWelcome, onRoomList, onJoined,
   *                     onLevel, onSnap, onEvent, onError, onOpen, onClose.
   */
  constructor(h) {
    this.h = h || {};
    this.ws = null;
    this.open = false;
    this.pid = 0;
    this.tickHz = 30;
    this.proto = 0;
    // 5.1: token — секрет, по которому сервер узнаёт ВЕРНУВШЕГОСЯ игрока.
    // Раньше опознание шло по имени: два Васи в комнате менялись телами, а
    // чужое имя достаточно было назвать. Держится в sessionStorage, потому
    // что чинить надо главный случай — F5 на своей же вкладке.
    this.token = '';
    try { this.token = sessionStorage.getItem('zza.token') || ''; } catch (e) { /* приватный режим */ }

    this.seq = 0;
    this.lastSent = null;

    this.pingId = 0;
    this.pingSentAt = new Map();
    this.rtt = 0;           // сглаженная оценка, мс
    this.rttLast = 0;
    this.rttMin = Infinity;

    this.bytesIn = 0;
    this.msgsIn = 0;
    this.snapsIn = 0;

    this._inputTimer = 0;
    this._pingTimer = 0;
    this._sampler = null;
    this._wantClose = false;
  }

  connect() {
    this._wantClose = false;
    const ws = new WebSocket(wsUrl());
    this.ws = ws;
    ws.onopen = () => {
      this.open = true;
      if (this.h.onOpen) this.h.onOpen();
    };
    ws.onclose = () => {
      this.open = false;
      this._stopTimers();
      if (this.h.onClose) this.h.onClose(this._wantClose);
    };
    // onerror в браузере не несёт полезного текста и всегда сопровождается
    // onclose; молчим, чтобы не сыпать в консоль пустых ошибок.
    ws.onerror = () => {};
    ws.onmessage = (ev) => this._onMessage(ev);
    return this;
  }

  close() {
    this._wantClose = true;
    this._stopTimers();
    if (this.ws) { try { this.ws.close(); } catch (e) { /* уже закрыт */ } }
    this.ws = null;
    this.open = false;
  }

  send(obj) {
    if (!this.open || !this.ws) return false;
    try {
      this.ws.send(JSON.stringify(obj));
      return true;
    } catch (e) {
      return false;
    }
  }

  // --- 5.1 -------------------------------------------------------------

  hello(name) { return this.send({ t: 'hello', name: name || '', ver: PROTO }); }
  rooms() { return this.send({ t: 'rooms' }); }
  // room === '' или отсутствует = создать новую (5.1)
  join(room, name) {
    return this.send({ t: 'join', room: room || '', name: name || '',
                       token: this.token || '' });
  }
  // Без этого не придёт ни level, ни снапшотов (5.1). Самая дорогая грабля
  // протокола: join кладёт в ЛОББИ, а не в игру.
  ready(v) { return this.send({ t: 'ready', v: !!v }); }
  // 8.7: параметры игры. Клиент ПРОСИТ — решает сервер, и ответом всегда
  // приходит joined с тем, что сервер на самом деле поставил.
  opts(o) { return this.send({ t: 'opts', opts: o || {} }); }
  // 8.7: кнопка хозяина. Не «игра пошла», а «хозяин сказал начать».
  start(v) { return this.send({ t: 'start', v: v === undefined ? true : !!v }); }
  leave() { return this.send({ t: 'leave' }); }

  sendInput(mv, aim, btn) {
    this.seq++;
    const m = { t: 'input', seq: this.seq, mv: mv, aim: aim, btn: btn };
    this.lastSent = m;
    this.send(m);
    return this.seq;
  }

  ping() {
    this.pingId = (this.pingId + 1) & 0x7fffffff;
    const ct = performance.now() / 1000;
    this.pingSentAt.set(this.pingId, performance.now());
    if (this.pingSentAt.size > 32) {
      const k = this.pingSentAt.keys().next().value;
      this.pingSentAt.delete(k);
    }
    this.send({ t: 'ping', id: this.pingId, ct: ct });
  }

  /**
   * Поток ввода 30 Гц (5.1). Частота фиксирована таймером, а не событиями
   * клавиатуры: зажатая клавиша в браузере сыплет автоповтором, и без
   * таймера это были бы сотни сообщений в секунду.
   * @param {function} sampler — вернуть {mv:[dx,dy], aim:[ax,ay], btn:int}
   */
  startPump(sampler) {
    this._sampler = sampler;
    this._stopTimers();
    this._inputTimer = setInterval(() => {
      if (!this.open || !this._sampler) return;
      const s = this._sampler();
      if (!s) return;
      const seq = this.sendInput(s.mv, s.aim, s.btn);
      if (this.h.onInputSent) this.h.onInputSent(seq, s.mv);
    }, Math.round(1000 / INPUT_HZ));
    this._pingTimer = setInterval(() => { if (this.open) this.ping(); }, PING_MS);
    if (this.open) this.ping();
  }

  _stopTimers() {
    if (this._inputTimer) { clearInterval(this._inputTimer); this._inputTimer = 0; }
    if (this._pingTimer) { clearInterval(this._pingTimer); this._pingTimer = 0; }
  }

  // --- 5.2 -------------------------------------------------------------

  _onMessage(ev) {
    const raw = ev.data;
    this.bytesIn += typeof raw === 'string' ? raw.length : (raw.size || 0);
    this.msgsIn++;
    let m;
    try {
      m = JSON.parse(raw);
    } catch (e) {
      return;                       // мусор с провода — молча роняем
    }
    if (!m || typeof m !== 'object') return;
    switch (m.t) {
      case 'welcome':
        this.pid = m.pid; this.tickHz = m.tick_hz || 30; this.proto = m.proto;
        // Свой token берём ОДИН раз. Каждое новое соединение получает новый,
        // и перезаписать им старый значило бы потерять право на свою
        // сущность ровно в тот момент, когда оно нужно, — при переподключении.
        if (m.token && !this.token) {
          this.token = m.token;
          try { sessionStorage.setItem('zza.token', m.token); } catch (e) { /* приватный режим */ }
        }
        if (this.h.onWelcome) this.h.onWelcome(m);
        break;
      case 'roomlist':
        if (this.h.onRoomList) this.h.onRoomList(m.rooms || []);
        break;
      case 'joined':
        if (this.h.onJoined) this.h.onJoined(m);
        break;
      case 'level': {
        // tiles — base64 RLE (proto.rle_encode)
        let tiles = null;
        try { tiles = rleDecode(m.tiles, m.w * m.h); } catch (e) { tiles = null; }
        if (this.h.onLevel) this.h.onLevel(m, tiles);
        break;
      }
      case 'snap':
        this.snapsIn++;
        if (this.h.onSnap) this.h.onSnap(m);
        break;
      case 'ev':
        if (this.h.onEvent) this.h.onEvent(m);
        break;
      // 11.7: набор апгрейдов. Отдельным сообщением именно потому, что
      // собирать его из событий pick клиенту запрещено (5.2).
      case 'build':
        if (this.h.onBuild) this.h.onBuild(m);
        break;
      case 'pong': {
        const sent = this.pingSentAt.get(m.id);
        if (sent !== undefined) {
          this.pingSentAt.delete(m.id);
          const rtt = performance.now() - sent;
          this.rttLast = rtt;
          if (rtt < this.rttMin) this.rttMin = rtt;
          // EMA: одиночный выброс не должен дёргать цифру на экране
          this.rtt = this.rtt ? this.rtt * 0.8 + rtt * 0.2 : rtt;
        }
        break;
      }
      case 'err':
        if (this.h.onError) this.h.onError(m);
        break;
      default:
        break;                      // незнакомый тип — не наше дело
    }
  }
}

export { RECONNECT_MS };
