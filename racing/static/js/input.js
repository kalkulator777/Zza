// Клавиатура клиента — раздел 10.4 контракта.
//
// Владелец модуля: [client]. Зависимость одна — биты кнопок из protocol.js.
//
// Модуль собирает битовую маску кнопок для бинарного пакета ввода (раздел 5.2)
// и ничего больше: ни физики, ни сети, ни интерфейса он не знает.
//
// РАСКЛАДКА НЕ ВЛИЯЕТ. Всё сопоставление идёт по `event.code` — физической
// позиции клавиши, — поэтому русская раскладка, Dvorak и Colemak работают
// одинаково. `event.key` здесь не используется нигде.
//
// ЧУЖИЕ КЛАВИШИ (12.6 и 12.11). Этот файл НЕ трогает:
//   Tab  и F3     — их слушают ui/hud.js и ui/perf.js;
//   C и Shift     — их слушает render/renderer.js.
// Двойная привязка сломала бы и то, и другое, поэтому бит BTN_LOOK_BACK
// заполняется не клавишей, а вызовом setLookBack(on): состояние «взгляд назад»
// живёт в рендере, а в протокол его кладёт main.js одной строкой.
//
// Использование из main.js:
//
//   import { createInput } from './input.js';
//
//   const input = createInput({ onEscape: () => leaveRoom() });
//   input.attach();                  // слушатели на window
//   input.setEnabled(racing);        // вне гонки маска всегда нулевая
//   // каждый фиксированный шаг:
//   input.setLookBack(renderer.lookBack);
//   const buttons = input.buttons;   // готовое число 0..255
//
// АЛЛОКАЦИИ. В кадровом цикле — ноль: `buttons` это поле, а не вычисление.
// Пересчёт маски происходит только в обработчиках keydown/keyup, то есть
// по действию человека, и стоит один проход по таблице из тринадцати строк.

import {
    BTN_THROTTLE,
    BTN_BRAKE,
    BTN_LEFT,
    BTN_RIGHT,
    BTN_DRIFT,
    BTN_ITEM,
    BTN_LOOK_BACK,
} from './protocol.js';

// ---------------------------------------------------------------------------
// Таблица привязок (раздел 10.4)
// ---------------------------------------------------------------------------
//
// Два параллельных массива вместо массива объектов: обработчик клавиши
// получает индекс строки, а дальше читает биты по индексу. Один и тот же бит
// может стоять у нескольких строк (W и «стрелка вверх»), поэтому маска
// собирается из массива зажатых клавиш, а не гасится по первому же keyup.

const BIND_CODES = [
    'KeyW', 'ArrowUp',          // газ
    'KeyS', 'ArrowDown',        // тормоз / задний ход
    'KeyA', 'ArrowLeft',        // влево
    'KeyD', 'ArrowRight',       // вправо
    'Space',                    // дрифт (ручник)
    'KeyE', 'ControlLeft', 'ControlRight',   // применить бонус
];

const BIND_BITS = [
    BTN_THROTTLE, BTN_THROTTLE,
    BTN_BRAKE, BTN_BRAKE,
    BTN_LEFT, BTN_LEFT,
    BTN_RIGHT, BTN_RIGHT,
    BTN_DRIFT,
    BTN_ITEM, BTN_ITEM, BTN_ITEM,
];

const BIND_COUNT = BIND_CODES.length;

// code -> индекс строки. Object.create(null) — чтобы 'constructor' и прочие
// имена из Object.prototype не выдавали себя за привязанную клавишу.
const BIND_INDEX = Object.create(null);
for (let i = 0; i < BIND_COUNT; i++) {
    BIND_INDEX[BIND_CODES[i]] = i;
}

// Клавиши, у которых у браузера есть своё поведение (прокрутка страницы
// пробелом и стрелками). Гасим его только когда ввод реально наш.
const SWALLOW = Object.create(null);
SWALLOW.Space = true;
SWALLOW.ArrowUp = true;
SWALLOW.ArrowDown = true;
SWALLOW.ArrowLeft = true;
SWALLOW.ArrowRight = true;

/** Идёт ли ввод в текстовое поле: там W — это буква, а не газ. */
function isTextTarget(node) {
    if (!node) return false;
    const tag = node.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return true;
    return node.isContentEditable === true;
}

export class Input {
    /**
     * @param {object} [options]
     *        target   — на ком слушать клавиши (по умолчанию window);
     *        onEscape — вызывается по Esc (раздел 10.4: «меню»);
     *        enabled  — начальное состояние, по умолчанию true.
     */
    constructor(options) {
        const opts = options || null;
        this.target = (opts && opts.target) || (typeof window !== 'undefined' ? window : null);
        this.onEscape = (opts && opts.onEscape) || null;
        this.enabled = !(opts && opts.enabled === false);

        // Зажатые клавиши по индексу строки таблицы.
        this.held = new Uint8Array(BIND_COUNT);
        this.mask = 0;          // маска без бита «взгляд назад»
        this.lookBack = false;
        this.buttons = 0;       // итоговая маска для encodeInput

        this.attached = false;

        // Обработчики создаются один раз здесь, а не при каждом attach():
        // повторные attach/detach не плодят замыканий.
        const self = this;
        this._onKeyDown = function (ev) { self._handleDown(ev); };
        this._onKeyUp = function (ev) { self._handleUp(ev); };
        this._onBlur = function () { self.releaseAll(); };
    }

    // --- жизненный цикл -----------------------------------------------------

    attach() {
        if (this.attached || !this.target) return;
        this.attached = true;
        this.target.addEventListener('keydown', this._onKeyDown);
        this.target.addEventListener('keyup', this._onKeyUp);
        this.target.addEventListener('blur', this._onBlur);
    }

    detach() {
        if (!this.attached || !this.target) return;
        this.attached = false;
        this.target.removeEventListener('keydown', this._onKeyDown);
        this.target.removeEventListener('keyup', this._onKeyUp);
        this.target.removeEventListener('blur', this._onBlur);
        this.releaseAll();
    }

    /**
     * Принимать ли ввод. Выключение немедленно отпускает все клавиши: иначе
     * зажатый на выходе в лобби газ «залипнет» до следующей гонки.
     */
    setEnabled(on) {
        const want = !!on;
        if (want === this.enabled) return;
        this.enabled = want;
        if (!want) this.releaseAll();
    }

    /**
     * Бит «взгляд назад» (5.2, бит 6). Клавишу Shift держит рендер (12.11),
     * сюда её состояние приносит main.js — так бит протокола заполнен,
     * а двойной привязки нет.
     */
    setLookBack(on) {
        const want = !!on;
        if (want === this.lookBack) return;
        this.lookBack = want;
        this.buttons = want ? (this.mask | BTN_LOOK_BACK) : this.mask;
    }

    /** Отпустить всё: потеря фокуса окна, выход из гонки, выключение ввода. */
    releaseAll() {
        const held = this.held;
        for (let i = 0; i < BIND_COUNT; i++) held[i] = 0;
        this.mask = 0;
        this.buttons = this.lookBack ? BTN_LOOK_BACK : 0;
    }

    // --- обработчики --------------------------------------------------------

    _handleDown(ev) {
        if (ev.code === 'Escape') {
            // Esc работает всегда: и при выключенном вводе, и из поля чата —
            // но из поля сначала просто снимает фокус.
            if (isTextTarget(ev.target)) {
                if (ev.target.blur) ev.target.blur();
                return;
            }
            if (this.onEscape) this.onEscape();
            return;
        }
        if (!this.enabled) return;
        if (isTextTarget(ev.target)) return;
        const index = BIND_INDEX[ev.code];
        if (index === undefined) return;
        if (SWALLOW[ev.code]) ev.preventDefault();
        if (this.held[index]) return;      // автоповтор ничего не меняет
        this.held[index] = 1;
        this._recompute();
    }

    _handleUp(ev) {
        const index = BIND_INDEX[ev.code];
        if (index === undefined) return;
        if (SWALLOW[ev.code]) ev.preventDefault();
        if (!this.held[index]) return;
        this.held[index] = 0;
        this._recompute();
    }

    /** Пересборка маски. Зовётся только по нажатию, в кадре её нет. */
    _recompute() {
        const held = this.held;
        let mask = 0;
        for (let i = 0; i < BIND_COUNT; i++) {
            if (held[i]) mask |= BIND_BITS[i];
        }
        this.mask = mask;
        this.buttons = this.lookBack ? (mask | BTN_LOOK_BACK) : mask;
    }
}

/** Фабрика — чтобы main.js не писал `new`. */
export function createInput(options) {
    return new Input(options);
}

export default Input;
