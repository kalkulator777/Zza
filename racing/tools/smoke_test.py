#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Приёмочный прогон игры: сервер, два браузера, одна гонка от меню до итогов.

Запуск из каталога ``racing/``::

    python3 tools/smoke_test.py                    # полный прогон
    python3 tools/smoke_test.py --verbose          # плюс подробности
    python3 tools/smoke_test.py --port 8123        # фиксированный порт
    python3 tools/smoke_test.py --keep-screenshots /tmp/shots
    python3 tools/smoke_test.py --no-browser       # только серверная часть

Код возврата:

    0 — всё цело (или браузерная часть пропущена: нет playwright);
    1 — игра сломана, список провалов напечатан;
    2 — не смог отработать сам тест: порт занят, сервер не поднялся,
        браузер не запустился. Это НЕ поломка игры.

ЧТО ЭТОТ СКРИПТ ПРОВЕРЯЕТ (и чего не проверяет ``tools/test_sim.py``)

``test_sim.py`` гоняет симуляцию и физику в чистом питоне. Здесь наоборот:
берётся настоящий сервер, настоящий клиент в настоящем браузере и сеть
между ними. Сценарий повторяет путь живого игрока — имя, комната, машина,
готовность, старт, отсчёт, руль, пауза, финиш, итоги, лобби, — а машину
ведёт бот, который жмёт настоящие клавиши через ``KeyboardEvent`` по
``event.code`` (раздел 10.4: раскладка не важна, важен код клавиши).

Проверяется:

* ноль ошибок в консоли браузера и ноль необработанных исключений страницы;
* машина едет: путь по трассе растёт, скорость выходит на разумные значения;
* круг засчитывается, время круга и лучший круг заполняются;
* каждый клиент видит машину другого, и позиции сходятся: предсказание
  против авторитета сервера и картинка чужой машины против него же;
* снапшоты идут 20 Гц (раздел 5.1);
* чужая машина едет гладко — ловится класс сетевых регрессий из 12.12;
* пауза морозит время гонки и времена кругов (12.12);
* сервер не выдал ни одного исключения;
* итоговая таблица приходит и непустая, возврат в лобби работает.

Прогон занимает около минуты: один круг на самой короткой трассе
(«Офисный круг», 1374 м) вдвоём, с проверкой паузы посередине.

ЧЕГО ЭТОТ СКРИПТ НЕ ПРОВЕРЯЕТ. Качество картинки и бюджет рендера — прогон
идёт на низких настройках графики и в маленьком окне, иначе программный
рендер headless-браузера не успевает (см. LOW_GFX). Число кадров в секунду
здесь печатается, но ничего не значит: это скорость стенда, а не игры. Также
не проверяются звук, бонусы и снаряды (в приёмочной гонке они выключены —
ради предсказуемого времени прогона), больше двух игроков, переподключение
и потеря связи, обнаружение серверов по UDP, чат, правка настроек комнаты
владельцем, зеркальные трассы, режим наблюдателя, поведение при плохой сети
и мобильные браузеры. Физику и симуляцию проверяет tools/test_sim.py —
здесь они не дублируются.

ЛОББИ И КОМНАТА ИДУТ ЧЕРЕЗ ``net.send``. Нажатия по кнопкам меню и лобби
здесь намеренно не эмулируются: разметку этих экранов строит JS, она живёт
своей жизнью, и тест ломался бы от каждой косметической правки. Вместо этого
шлются ровно те же JSON-события, что шлют обработчики main.js (раздел 5.4),
а проверяется то, что в ответ происходит с экранами и состоянием клиента.
Руль и пауза — наоборот, только настоящими клавишами: это и есть предмет
проверки.

ЗАВИСИМОСТИ. Стандартная библиотека, Tornado и playwright. Playwright —
инструмент разработки, на игровых машинах его нет и не будет: без него
браузерная часть честно пропускается, а серверная прогоняется двумя
websocket-клиентами Tornado.

ГРАБЛИ, НА КОТОРЫХ УЖЕ ОБЖИГАЛИСЬ. Бинарный ввод шлётся только бинарным
кадром: ``write_message(payload, binary=True)``. Текстовый кадр с теми же
байтами сервер штатно считает нарушением протокола и рвёт связь, а выглядит
это как загадочный обрыв.
"""

import argparse
import asyncio
import json
import math
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------------------
# Пороги. Каждый — с обоснованием: откуда взято число.
# ---------------------------------------------------------------------------

RACE_TRACK = 'office'          # самая короткая и простая трасса, 1374 м
RACE_LAPS = 1                  # один круг: весь прогон должен уложиться в ~1,5 мин
HOST_CAR = 'hatch'
GUEST_CAR = 'buggy'

DRIVE_BEFORE_PAUSE = 7.0       # с езды до проверки паузы
PAUSE_HOLD = 1.6               # с, сколько держим паузу
RACE_DEADLINE = 110.0          # с на всю гонку, дальше признаём зависшей
STAGE_TIMEOUT = 15.0           # с на любой шаг лобби

# Скорость: хэтч и багги упираются в 50+ м/с, но круг на «Офисном» —
# это повороты. 15 м/с (54 км/ч) — граница «машина поехала», а не «елозит».
MIN_TOP_SPEED = 15.0
# Путь по трассе за гонку: круг 1374 м, берём с большим запасом на случай,
# если трассу в комнате поменяют.
MIN_PROGRESS = 300.0
# Откат пути назад: больше — это телепорт или срезка, а не откат из отбойника.
MAX_ROLLBACK = 40.0

# Снапшоты: раздел 5.1 требует 20 Гц (каждый третий тик 60 Гц).
SNAPSHOT_HZ_MIN = 17.0
SNAPSHOT_HZ_MAX = 23.0

PAUSE_GAP_MS = 1000.0          # простой длиннее — это пауза, а не потеря

# Сходимость позиций. Вопрос «видит ли хозяин гостя там, где тот есть»
# разложен на две половины, и каждая меряется по серверному тику, а не по
# времени кадра, — поэтому тормоза стенда в них не попадают:
#
#   1. гость против сервера: расхождение собственного предсказания
#      с авторитетным состоянием того же тика. Это то самое, что net.js
#      считает на каждом снапшоте (10.2): ниже RECONCILE_EPS = 0,05 м
#      коррекция не нужна вовсе, измеренная там медиана — 0,0094 м;
#   2. хозяин против сервера: где хозяин рисует гостя против того, где гость
#      был по данным сервера в этот момент. Здесь и живёт буфер интерполяции
#      в INTERP_DELAY = 100 мс: ради него картинка чужой машины отстаёт,
#      и он же не даёт ей разъехаться.
#
# Сумма двух и есть «где гость себя предсказал против того, где его видит
# хозяин» — те самые «порядка полуметра»; она печатается справочной строкой.
#
# Откуда пороги. Первая половина на этом стенде даёт медиану 0,002 м и p95
# около 0,5 м: хвост — это пропущенные шаги главного цикла, поэтому порог
# на нём стоит грубый, он тут только страховкой, а работает медиана.
# Вторая половина даёт медиану 0,003 м и p95 0,010 м — больше и не может:
# 0,008 м это стрелка прогиба хорды за 50 мс между снапшотами при предельном
# поперечном ускорении. Пороги ниже — примерно вдесятеро от измеренного.
# Проверено на подсаженной регрессии 12.12 (историю чужих машин разметили
# временем прихода пакета вместо тика): p95 второй половины уезжает
# на 0,28 м, то есть за порог.
PREDICT_MEDIAN_MAX = 0.20
PREDICT_P95_MAX = 3.00         # хвост на медленном стенде шумит: 0,3..1,3 м
                               # от кадра к кадру, поэтому он тут страховка
                               # от грубой поломки, а работает медиана
PREDICT_OVER_EPS_MAX = 0.40    # доля снапшотов, где потребовалась коррекция
PREDICT_EPS = 0.05             # с какого расхождения коррекция считается нужной

# Тот же допуск для --physics rapier. Доля остаётся прежней (0,40), меняется
# РАССТОЯНИЕ, и оно выведено из замера, а не подобрано под прогон.
#
# Вывод. У классики клиент повторяет сервер до бита: неустранимый шум отката
# равен нулю, и 5 см — это чистый допуск поверх нуля. У Rapier откат не бит
# в бит (§8.3 разведки), и неустранимый шум НЕПРЕРЫВНОЙ переигровки измерен
# на том же модуле: откат на 5 тиков каждые 3 тика, 2400 тиков, медиана
# 0,0169 м, p99 0,0298 м, ПОТОЛОК 0,0305 м, дальше не растёт. Равный по
# смыслу допуск — потолок шума плюс те же 5 см: 0,031 + 0,05 = 0,081 ≈ 0,08.
#
# Запас замерен: при 0,08 м доля держится около 0,17–0,19 при пороге 0,40,
# то есть вдвое. Грубая поломка предсказания уводит её за 0,90.
PREDICT_EPS_RAPIER = 0.08

PHYSICS = 'classic'
DIVERGE_MEDIAN_MAX = 0.05
DIVERGE_P95_MAX = 0.10

# Плавность чужой машины: насколько видимая скорость (пройденное за кадр
# расстояние, делённое на длину кадра) расходится с заявленной в снапшоте.
# Метрика взята из 10.2, где ею мерили ровно ту регрессию с дёрганьем:
# «разметка по времени прихода давала 51 % кадров с отклонением скорости
# больше половины, разметка по tick — 1,3 %». Порог по доле таких кадров —
# 5 %, между этими числами ближе к хорошему краю. Второй порог, по p95
# самого отклонения, чувствительнее: целая игра даёт 1..2 %, подсаженная
# регрессия 12.12 — 19..24 %.
#
# Почему не отклонение от гладкой траектории в метрах, как в таблице 12.12:
# оно растёт как куб длины кадра, а headless-браузер выдаёт 15 кадров
# в секунду вместо 60 — те же 0,0017 м превращаются в 0,13 м, и порог
# пришлось бы привязывать к скорости стенда. Это число печатается справочно.
SMOOTH_JUMPY_MAX = 0.05        # доля кадров с отклонением скорости > 50 %
SMOOTH_P95_MAX = 0.10          # p95 самого отклонения, доля единицы
SMOOTH_MAX_FRAME_MS = 120.0    # окна с кадром длиннее в метрику не идут

# Пауза морозит серверный тик целиком (12.12), значит время гонки и время
# круга не должны сдвинуться вообще. 0,05 с — запас на то, что замеры берутся
# двумя отдельными вызовами в браузер.
PAUSE_DRIFT_MAX = 0.05

# Окно браузера и настройки графики для прогона. Замерено на этой машине:
# 1000x640 на среднем пресете — 4,5 кадра/с и 360 пропущенных шагов за 6 с;
# 480x320 на низком с масштабом 0,5 — 31 кадр/с и 15 пропущенных (в одиночку;
# вдвоём браузеры делят процессор, и выходит около 15 кадров/с на каждого).
BROWSER_WIDTH = 480
BROWSER_HEIGHT = 320
LOW_GFX = {
    'racing.quality': 'low',
    'racing.gfx.renderscale': '0.5',
    'racing.gfx.decor': 'sparse',
    'racing.gfx.decoranim': '0',
    'racing.gfx.particles': 'off',
    'racing.gfx.ao': '0',
    'racing.gfx.glow': '0',
    'racing.gfx.viewdistance': 'near',
}

# Где искать chromium: playwright install здесь запускать нельзя.
CHROMIUM_CANDIDATES = (
    '/opt/pw-browsers/chromium-1194/chrome-linux/chrome',
    '/opt/pw-browsers/chromium/chrome-linux/chrome',
)


class TestBroken(Exception):
    """Сломалось окружение, а не игра: занятый порт, браузер, playwright."""


class GameBroken(Exception):
    """Сломалась игра, и продолжать прогон бессмысленно."""


# ---------------------------------------------------------------------------
# Отчёт
# ---------------------------------------------------------------------------

class Report(object):
    """Печатает строки по ходу дела и помнит, что провалилось."""

    def __init__(self, verbose=False):
        self.verbose = verbose
        self.failures = []
        self.skips = []
        self.checks = 0
        self.started = time.monotonic()

    def ok(self, text):
        self.checks += 1
        print('[ок]      %s' % text, flush=True)

    def fail(self, text):
        self.checks += 1
        self.failures.append(text)
        print('[ПРОВАЛ]  %s' % text, flush=True)

    def check(self, condition, text):
        if condition:
            self.ok(text)
        else:
            self.fail(text)
        return bool(condition)

    def skip(self, text):
        self.skips.append(text)
        print('[пропуск] %s' % text, flush=True)

    def info(self, text):
        print('[инфо]    %s' % text, flush=True)

    def note(self, text):
        """Подробность: видна только с --verbose."""
        if self.verbose:
            print('          %s' % text, flush=True)

    def stage(self, text):
        print('\n--- %s' % text, flush=True)

    def summary(self, broken=None):
        """Сводка и код возврата: 0 — цело, 1 — игра, 2 — сам прогон."""
        seconds = time.monotonic() - self.started
        print('')
        print('=' * 68)
        if self.failures:
            print('  СЛОМАНО: %d из %d проверок, %.0f с' %
                  (len(self.failures), self.checks, seconds))
            for text in self.failures:
                print('    - %s' % text)
            if broken:
                print('  Прогон остановлен на этом месте: %s' % broken)
        elif broken:
            print('  ТЕСТ НЕ ОТРАБОТАЛ: %s' % broken)
            print('  Это поломка прогона или окружения, а не игры.')
            print('  Успело пройти проверок: %d, %.0f с' % (self.checks, seconds))
        else:
            print('  ЦЕЛО: %d проверок пройдено, %.0f с' % (self.checks, seconds))
        for text in self.skips:
            print('  пропущено: %s' % text)
        print('=' * 68, flush=True)
        if self.failures:
            return 1
        return 2 if broken else 0


# ---------------------------------------------------------------------------
# Сервер
# ---------------------------------------------------------------------------

# Метрики плавности и сходимости меряют, УСПЕВАЕТ ЛИ клиент, поэтому на занятой
# машине они честно краснеют при целой игре. Измерено на десяти чередующихся
# прогонах: 40 кадров/с и выше — зелено 4 из 4, 35 и ниже — красно 6 из 6,
# причём чистый HEAD падает с той же подписью. Порог по загрузке выведен оттуда же.
LOAD_LIMIT = 1.0


def machine_load():
    """Средняя загрузка за минуту, или None если прочитать нечем."""
    try:
        with open('/proc/loadavg', 'r') as fp:
            return float(fp.read().split()[0])
    except Exception:
        return None


def warn_if_busy():
    """Предупредить, что на занятой машине красный результат недостоверен."""
    load = machine_load()
    if load is None:
        return
    if load > LOAD_LIMIT:
        print('  ВНИМАНИЕ: загрузка машины %.1f при пороге %.1f.' % (load, LOAD_LIMIT),
              flush=True)
        print('  Метрики плавности и сходимости на занятой машине краснеют'
              ' при целой игре.', flush=True)
        print('  Красный результат сейчас НЕ доказательство поломки —'
              ' дождитесь спада и перепрогоните.', flush=True)
    elif load >= 0.0:
        print('  загрузка машины %.1f — можно верить результату' % load, flush=True)


def free_port():
    """Свободный порт от системы. 8000 не трогаем — там может идти игра."""
    sock = socket.socket()
    try:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def port_is_busy(port):
    sock = socket.socket()
    sock.settimeout(0.5)
    try:
        return sock.connect_ex(('127.0.0.1', port)) == 0
    finally:
        sock.close()


class Server(object):
    """Настоящий run.py в отдельном процессе, с перехватом вывода."""

    # Тornado с pretty logging печатает ошибки как «[E 260914 18:35:57 ...]».
    ERROR_RE = re.compile(r'^\[E \d', re.M)

    def __init__(self, port, verbose=False, physics=None):
        self.port = port
        self.verbose = verbose
        # Какой физикой сервер считает гонку (§12.22, §12.24). None — не
        # передавать ключ вовсе, то есть ровно прежний прогон.
        self.physics = physics
        self.proc = None
        self.lines = []
        self._reader = None

    def start(self):
        if port_is_busy(self.port):
            raise TestBroken('порт %d занят: там уже кто-то слушает. '
                             'Укажите другой ключом --port.' % self.port)
        cmd = [sys.executable, 'run.py', '--no-browser',
               '--port', str(self.port), '--bind', '127.0.0.1',
               '--name', 'Приёмка']
        if self.physics:
            cmd += ['--physics', self.physics]
        try:
            self.proc = subprocess.Popen(
                cmd, cwd=BASE_DIR, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1)
        except OSError as exc:
            raise TestBroken('не смог запустить run.py: %s' % exc)

        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

        url = 'http://127.0.0.1:%d/' % self.port
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                # Питон упал с трассировкой — это поломка игры, а не стенда.
                text = '\n'.join(self.lines)
                if 'Traceback' in text or 'Error' in text:
                    raise GameBroken('сервер не запустился: %s'
                                     % self.last_error())
                raise TestBroken('сервер умер на старте, код %d: %s'
                                 % (self.proc.returncode, self.tail(3)))
            try:
                with urllib.request.urlopen(url, timeout=1.0) as resp:
                    if resp.status == 200:
                        return
            except Exception:
                time.sleep(0.1)
        raise TestBroken('сервер не ответил на %s за 20 с' % url)

    def _pump(self):
        try:
            for line in self.proc.stdout:
                self.lines.append(line.rstrip('\n'))
                if self.verbose:
                    print('          [сервер] %s' % line.rstrip('\n'), flush=True)
        except Exception:
            pass

    def tail(self, count=30):
        return ' | '.join(line.strip() for line in self.lines[-count:]
                          if line.strip())

    def last_error(self):
        """Последняя осмысленная строка лога — обычно сам текст исключения."""
        for line in reversed(self.lines):
            text = line.strip()
            if text and not text.startswith(('File "', '^', '~', 'Traceback')):
                return text
        return self.tail(3)

    def errors(self):
        """Строки лога, похожие на исключение сервера."""
        return [line.strip() for line in self.lines
                if self.ERROR_RE.match(line) or line.startswith('Traceback')]

    def stop(self):
        if self.proc is None:
            return
        if self.proc.poll() is None:
            try:
                self.proc.send_signal(signal.SIGINT)
                self.proc.wait(timeout=5)
            except Exception:
                try:
                    self.proc.kill()
                    self.proc.wait(timeout=5)
                except Exception:
                    pass
        self.proc = None


def check_static(report, port):
    """Все файлы клиента отдаются сервером: 200 и непустое тело."""
    files = ['']
    for root, _dirs, names in os.walk(os.path.join(BASE_DIR, 'static')):
        for name in sorted(names):
            if not name.endswith(('.js', '.css', '.html')):
                continue
            path = os.path.relpath(os.path.join(root, name),
                                   os.path.join(BASE_DIR, 'static'))
            if path == 'index.html':
                continue
            files.append(path.replace(os.sep, '/'))

    bad = []
    total = 0
    for path in files:
        url = 'http://127.0.0.1:%d/%s' % (port, path)
        try:
            with urllib.request.urlopen(url, timeout=3.0) as resp:
                body = resp.read()
                total += len(body)
                if resp.status != 200 or not body:
                    bad.append('%s -> %d, %d байт' % (path or '/', resp.status, len(body)))
        except Exception as exc:
            bad.append('%s -> %s' % (path or '/', exc))

    report.check(not bad, 'статика отдана: %d файлов, %d КБ, ошибок %d%s'
                 % (len(files), total // 1024, len(bad),
                    ('; ' + '; '.join(bad[:3])) if bad else ''))


# ---------------------------------------------------------------------------
# Агент страницы: бот за рулём и запись замеров
# ---------------------------------------------------------------------------
#
# Ставится один раз после загрузки страницы. Ничего в игре не меняет:
# обработчики событий сети оборачиваются (старый вызывается следом),
# net.interpolate оборачивается ради точного момента кадра — main.js зовёт
# его раз в кадр и передаёт тот самый момент, на который рисуется кадр.

PAGE_AGENT = r"""
(opts) => {
  const R = window.__racing;
  if (!R || !R.net || !R.app) return 'на странице нет window.__racing';
  if (window.__smoke) return 'ok';

  const net = R.net;
  const app = R.app;
  const DT = 1.0 / 60.0;
  const INTERP_DELAY = 100.0;     // мс, раздел 10.2
  const GRIP_LAT_ACCEL = 160.0;   // physics.js
  const GRIP_SAFETY = 0.72;       // бот едет не по идеальной траектории
  const BRAKE_SAFETY = 0.70;
  const SCAN_STEP = 8.0;          // м между точками просмотра вперёд

  const FLAG_FINISHED = 1 << 5;    // раздел 5.3, флаги машины

  const S = {
    lane: opts.lane || 0.0,
    drive: false,
    other: -1,            // слот чужой машины, за которой следим
    otherDone: 0,         // чужая машина финишировала — дальше она призрак
    frames: 0,            // кадров гонки со своей живой машиной
    both: 0,              // из них те, где чужая ещё в гонке
    seen: 0,              // из них с видимой чужой машиной
    firstMs: 0, lastMs: 0,
    progress: -1e9,       // путь по трассе, максимум за гонку, м
    progStart: 1e9,       // он же на первом кадре: решётка стоит до линии
    progressSeries: [],   // он же изредка — проверить, что растёт
    maxSpeed: 0.0,
    // своя машина: момент по серверной шкале (с) и показанная позиция
    lt: [], lx: [], lz: [],
    // чужая машина: момент показа по серверной шкале (с) и позиция
    vt: [], vx: [], vz: [], vvx: [], vvz: [], vms: [],
    // своя машина по данным сервера: момент (с гонки) и позиция из снапшота
    at: [], ax: [], az: [], rec: [],
    snapMs: [],           // приход снапшотов, performance.now()
    countdown: [], pauses: [], events: [], errors: [],
    room: null, rooms: null, results: null, welcome: null,
    radii: null, radiiFor: '',
    held: Object.create(null),
    keys: 0,              // сколько клавишных событий отправлено
    stuckSince: 0, reverseUntil: 0,
  };
  window.__smoke = S;

  // --- перехват событий сети (колбэки раздела 9) ---------------------------
  const h = net.h;
  function wrap(name, fn) {
    const prev = h[name];
    h[name] = function (msg) {
      try { fn(msg); } catch (e) { /* запись не имеет права ломать игру */ }
      if (prev) return prev.call(this, msg);
    };
  }
  wrap('onWelcome', (m) => { S.welcome = m; });
  wrap('onRoom', (m) => { S.room = m; });
  wrap('onRooms', (m) => { S.rooms = m; });
  wrap('onCountdown', (m) => { S.countdown.push({ v: m.value, ms: performance.now() }); });
  wrap('onPause', (m) => {
    S.pauses.push({ phase: m.phase, by: m.by, reason: m.reason,
                    budget: m.budget, ms: performance.now() });
  });
  wrap('onRaceEvent', (m) => { S.events.push(m); });
  wrap('onResults', (m) => { S.results = m; });
  wrap('onError', (m) => { S.errors.push(m); });
  wrap('onSnapshot', (snap) => {
    S.snapMs.push(performance.now());
    // Авторитетная позиция своей машины и ошибка предсказания на этот тик.
    // Время здесь — серверный тик, а не момент прихода, поэтому длинный кадр
    // страницы шкалу не портит: замер остаётся годным и на медленном стенде.
    if (!app.racing) return;
    const local = net.localSlot;
    if (local < 0) return;
    const i = snap.indexBySlot[local];
    if (i < 0 || (snap.carFlags[i] & FLAG_FINISHED)) return;
    S.at.push(snap.tick * DT);
    S.ax.push(snap.carX[i]);
    S.az.push(snap.carZ[i]);
    S.rec.push(net.reconcileError);
  });

  // --- запись кадра --------------------------------------------------------
  const origInterpolate = net.interpolate;
  net.interpolate = function (nowMs) {
    const out = origInterpolate.call(this, nowMs);
    try { sample(nowMs); } catch (e) { /* то же самое */ }
    return out;
  };

  function sample(nowMs) {
    if (!app.racing || !app.raceBuilt) return;
    const local = net.localSlot;
    if (local < 0) return;
    // Финишировавшая машина доживает призраком и через 3 с пропадает
    // (раздел 9): ни ввода, ни предсказания у неё уже нет, замерять нечего.
    if (!net.viewPresent[local] || (net.viewFlags[local] & FLAG_FINISHED)) return;

    S.frames++;
    if (S.firstMs === 0) S.firstMs = nowMs;
    S.lastMs = nowMs;
    const paused = net.pausePhase !== 'running';

    // Путь по трассе — накопленный шагом физики (track.advanceProgress).
    // net.progress[] для этого не годится: он свёрнут в круг для расчёта
    // отставания и на первом круге уходит в минус.
    const prog = net.state.progress;
    if (S.progStart > 1e8) S.progStart = prog;
    if (prog > S.progress) S.progress = prog;
    if (S.frames % 15 === 0) S.progressSeries.push(prog);

    // Своя машина: показанная позиция и момент, на который она посчитана.
    // Кольцо предсказаний размечено по тику сервера (10.2), поэтому после
    // шага seq состояние — это тик seq, а кадр рисуется между seq-1 и seq.
    const alpha = app.accumulator / DT;
    if (!paused) {
      S.lt.push((net.seq - 1 + alpha) * DT);
      S.lx.push(net.renderX(alpha));
      S.lz.push(net.renderZ(alpha));
      const st = net.state;
      const v = Math.sqrt(st.vx * st.vx + st.vz * st.vz);
      if (v > S.maxSpeed) S.maxSpeed = v;
    }

    // Чужая машина: момент показа — now - INTERP_DELAY по местным часам,
    // переведённый в серверную шкалу (та же арифметика, что в net._stampOf).
    const other = S.other;
    if (other < 0) return;
    if (net.viewPresent[other] && (net.viewFlags[other] & FLAG_FINISHED)) S.otherDone = 1;
    if (S.otherDone) return;
    S.both++;
    if (!net.viewPresent[other]) return;
    S.seen++;
    if (paused) return;
    S.vt.push((net._localMs(nowMs) - net.clockOffset - INTERP_DELAY) * 0.001);
    S.vx.push(net.viewX[other]);
    S.vz.push(net.viewZ[other]);
    S.vvx.push(net.viewVx[other]);
    S.vvz.push(net.viewVz[other]);
    S.vms.push(nowMs);
  }

  // --- клавиши -------------------------------------------------------------
  // Только настоящие события по event.code (раздел 10.4): input.js смотрит
  // именно на код клавиши, а не на символ.
  const KEY_NAME = { KeyW: 'w', KeyA: 'a', KeyS: 's', KeyD: 'd', KeyP: 'p' };

  function keyEvent(type, code) {
    S.keys++;
    window.dispatchEvent(new KeyboardEvent(type, {
      code: code, key: KEY_NAME[code] || code,
      bubbles: true, cancelable: true,
    }));
  }

  function press(code) {
    if (S.held[code]) return;
    S.held[code] = 1;
    keyEvent('keydown', code);
  }

  function release(code) {
    if (!S.held[code]) return;
    delete S.held[code];
    keyEvent('keyup', code);
  }

  function releaseAll() {
    for (const code in S.held) release(code);
  }

  S.tap = function (code) {      // одиночное нажатие: пауза
    keyEvent('keydown', code);
    keyEvent('keyup', code);
  };

  // --- бот -----------------------------------------------------------------
  // Осевая линия, точка прицеливания впереди, подруливание к своей полосе,
  // скорость по кривизне на тормозной дистанции. Тот же закон, что у бота
  // в tools/test_sim.py: держать только газ нельзя — упрёшься в отбойник.

  function computeRadii(track) {
    const n = track.count;
    const out = new Float64Array(n);
    for (let i = 0; i < n; i++) {
      const a = (i - 2 + n) % n, b = i, c = (i + 2) % n;
      const ab = Math.hypot(track.cx[b] - track.cx[a], track.cz[b] - track.cz[a]);
      const bc = Math.hypot(track.cx[c] - track.cx[b], track.cz[c] - track.cz[b]);
      const ca = Math.hypot(track.cx[a] - track.cx[c], track.cz[a] - track.cz[c]);
      const area2 = Math.abs((track.cx[b] - track.cx[a]) * (track.cz[c] - track.cz[a])
                           - (track.cx[c] - track.cx[a]) * (track.cz[b] - track.cz[a]));
      out[i] = area2 < 1e-9 ? 1e6 : ab * bc * ca / (2.0 * area2);
    }
    return out;
  }

  function topSpeedOf(stats) {
    const a = stats.drag;
    const b = stats.roll + stats.engineForce / stats.maxSpeed;
    const c = -stats.engineForce;
    return (-b + Math.sqrt(b * b - 4.0 * a * c)) / (2.0 * a);
  }

  function drive() {
    const track = net.track;
    const stats = net.localStats;
    const st = net.state;
    if (!track || !stats) return;
    if (S.radii === null || S.radiiFor !== track.id) {
      S.radii = computeRadii(track);
      S.radiiFor = track.id;
    }

    const n = track.count;
    const step = track.step;
    const speed = Math.sqrt(st.vx * st.vx + st.vz * st.vz);
    const idx = track.nearestIndex(st.x, st.z, st.sampleIdx);

    // Точка прицеливания: осевая впереди, сдвинутая в свою полосу.
    const look = 7.0 + speed * 0.55;
    const j = (idx + Math.round(look / step)) % n;
    const tx = track.cx[j] + track.cnx[j] * S.lane * track.chw[j];
    const tz = track.cz[j] + track.cnz[j] * S.lane * track.chw[j];
    let err = Math.atan2(tx - st.x, tz - st.z) - st.yaw;
    while (err > Math.PI) err -= Math.PI * 2.0;
    while (err < -Math.PI) err += Math.PI * 2.0;

    // Предел скорости по самой крутой дуге на тормозной дистанции.
    const latAccel = stats.gripStep * GRIP_LAT_ACCEL * GRIP_SAFETY;
    const brakeA = stats.brakeForce * BRAKE_SAFETY;
    let limit = topSpeedOf(stats);
    const reach = speed * speed / (2.0 * brakeA) + 3.0 * SCAN_STEP;
    for (let dist = SCAN_STEP; dist <= reach; dist += SCAN_STEP) {
      const k = (idx + Math.floor(dist / step)) % n;
      const corner = Math.sqrt(latAccel * (S.radii[k] + track.chw[k]));
      const allow = Math.sqrt(corner * corner + 2.0 * brakeA * dist);
      if (allow < limit) limit = allow;
    }
    if (st.boostTime > 0.0) limit = stats.boostSpeed;

    // Упёрлись — сдаём назад и в другую сторону, как живой игрок.
    // Счёт по времени, а не по кадрам: кадр в headless-браузере длится
    // 60..80 мс, и «сорок кадров назад» превратились бы в три секунды.
    const now = performance.now();
    if (now < S.reverseUntil) {
      release('KeyW');
      press('KeyS');
      if (err > 0.0) { release('KeyA'); press('KeyD'); }
      else { release('KeyD'); press('KeyA'); }
      return;
    }
    if (speed < 2.0 && st.spinTime <= 0.0) {
      if (S.stuckSince === 0) S.stuckSince = now;
      else if (now - S.stuckSince > 1000.0) {
        S.stuckSince = 0;
        S.reverseUntil = now + 700.0;
      }
    } else {
      S.stuckSince = 0;
    }

    if (speed < limit) { release('KeyS'); press('KeyW'); }
    else if (speed > limit * 1.12) { release('KeyW'); press('KeyS'); }
    else { release('KeyW'); release('KeyS'); }

    if (err > 0.030) { release('KeyD'); press('KeyA'); }
    else if (err < -0.030) { release('KeyA'); press('KeyD'); }
    else { release('KeyA'); release('KeyD'); }
  }

  function loop() {
    requestAnimationFrame(loop);
    if (!S.drive || !app.racing || !app.raceBuilt
        || net.pausePhase !== 'running' || net.localSlot < 0) {
      releaseAll();
      return;
    }
    try { drive(); } catch (e) { releaseAll(); }
  }
  requestAnimationFrame(loop);

  return 'ok';
}
"""


# ---------------------------------------------------------------------------
# Числовые помощники
# ---------------------------------------------------------------------------

def percentile(values, share):
    """Процентиль по отсортированному списку (без numpy — его здесь нет)."""
    if not values:
        return 0.0
    data = sorted(values)
    if len(data) == 1:
        return data[0]
    pos = share * (len(data) - 1)
    low = int(math.floor(pos))
    high = min(low + 1, len(data) - 1)
    frac = pos - low
    return data[low] * (1.0 - frac) + data[high] * frac


def interp_track(times, xs, zs, moment):
    """Позиция из записанной траектории на заданный момент; None — вне записи."""
    if not times or moment < times[0] or moment > times[-1]:
        return None
    lo, hi = 0, len(times) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if times[mid] <= moment:
            lo = mid
        else:
            hi = mid
    span = times[hi] - times[lo]
    if span <= 0:
        return (xs[lo], zs[lo])
    k = (moment - times[lo]) / span
    return (xs[lo] + (xs[hi] - xs[lo]) * k, zs[lo] + (zs[hi] - zs[lo]) * k)


def _parabola_at_zero(ts, vs):
    """МНК-парабола по точкам (ts, vs), её значение в t = 0. None — вырождено."""
    s0 = s1 = s2 = s3 = s4 = 0.0
    y0 = y1 = y2 = 0.0
    for t, v in zip(ts, vs):
        t2 = t * t
        s0 += 1.0
        s1 += t
        s2 += t2
        s3 += t2 * t
        s4 += t2 * t2
        y0 += v
        y1 += v * t
        y2 += v * t2
    a11, a12, a13 = s0, s1, s2
    a21, a22, a23 = s1, s2, s3
    a31, a32, a33 = s2, s3, s4
    det = (a11 * (a22 * a33 - a23 * a32)
           - a12 * (a21 * a33 - a23 * a31)
           + a13 * (a21 * a32 - a22 * a31))
    if abs(det) < 1e-18:
        return None
    # Нужен только свободный член: значение параболы в нуле.
    det_c = (y0 * (a22 * a33 - a23 * a32)
             - a12 * (y1 * a33 - a23 * y2)
             + a13 * (y1 * a32 - a22 * y2))
    return det_c / det


def smoothness(times, xs, zs, ms):
    """Отклонение показанной позиции от гладкой траектории, метры (12.12).

    По четырём соседним кадрам (два до, два после) проводится парабола —
    она в точности описывает движение с постоянным ускорением, то есть любую
    нормальную езду, — и с ней сравнивается то, что показано в среднем кадре.
    Сам средний кадр в подгонку не входит, иначе выброс частично затянул бы
    саму параболу.

    Почему не «середина между соседями», как напрашивается: headless-браузер
    выдаёт около 15 кадров в секунду, и на таком кадре настоящее поперечное
    ускорение в повороте даёт до 0,06 м отклонения от прямой — больше, чем
    ловимая регрессия. Парабола этот член снимает, остаётся рывок (третья
    производная), а он на порядок меньше.

    Окна, где хоть один кадр длиннее SMOOTH_MAX_FRAME_MS, выбрасываются:
    просадка кадра — это не сетевая регрессия.
    """
    out = []
    for i in range(2, len(times) - 2):
        window = range(i - 2, i + 2)
        if max(ms[k + 1] - ms[k] for k in window) > SMOOTH_MAX_FRAME_MS:
            continue
        base = times[i]
        ts = [times[k] - base for k in (i - 2, i - 1, i + 1, i + 2)]
        px = _parabola_at_zero(ts, [xs[k] for k in (i - 2, i - 1, i + 1, i + 2)])
        pz = _parabola_at_zero(ts, [zs[k] for k in (i - 2, i - 1, i + 1, i + 2)])
        if px is None or pz is None:
            continue
        out.append(math.hypot(xs[i] - px, zs[i] - pz))
    return out


def speed_deviation(vt, vx, vz, vvx, vvz, ms):
    """Отклонение видимой скорости чужой машины от заявленной, доля единицы.

    Метрика раздела 10.2: там ей мерили ту самую регрессию с дёрганьем
    («разметка по времени прихода давала 51 % кадров с отклонением скорости
    больше половины, разметка по tick — 1,3 %»). Видимая скорость — это
    пройденное за кадр расстояние, делённое на длительность кадра; заявленная
    приходит в снапшоте. Замершая и прыгнувшая машина даёт то ноль, то двойную
    скорость, и это видно при любой частоте кадров — в отличие от отклонения
    от гладкой траектории, которое растёт как куб длины кадра.
    """
    out = []
    for i in range(1, len(vt)):
        span = vt[i] - vt[i - 1]
        if span <= 0 or (ms[i] - ms[i - 1]) > SMOOTH_MAX_FRAME_MS:
            continue
        told = 0.5 * (math.hypot(vvx[i], vvz[i]) + math.hypot(vvx[i - 1], vvz[i - 1]))
        if told < 3.0:
            continue                      # на малой скорости доля шумит
        shown = math.hypot(vx[i] - vx[i - 1], vz[i] - vz[i - 1]) / span
        out.append(abs(shown - told) / told)
    return out


def snapshot_hz(stamps):
    """Частота снапшотов, Гц, по среднему интервалу.

    Не по медиане: занятый главный поток страницы разбирает очередь сообщений
    пачкой, и половина интервалов оказывается по десятой доле миллисекунды —
    медиана после такого показывает тысячи герц, а среднее остаётся честным.
    Простои длиннее PAUSE_GAP_MS выброшены: это пауза гонки, там сервер
    не тикает и снапшотов не шлёт.
    """
    gaps = [b - a for a, b in zip(stamps, stamps[1:]) if 0.0 < b - a < PAUSE_GAP_MS]
    if len(gaps) < 20:
        return 0.0
    return 1000.0 * len(gaps) / sum(gaps)


# ---------------------------------------------------------------------------
# Браузерный клиент
# ---------------------------------------------------------------------------

def find_chromium():
    """Путь к предустановленному chromium. playwright install не запускаем."""
    env = os.environ.get('SMOKE_CHROMIUM')
    if env and os.path.exists(env):
        return env
    for path in CHROMIUM_CANDIDATES:
        if os.path.exists(path):
            return path
    root = os.environ.get('PLAYWRIGHT_BROWSERS_PATH') or '/opt/pw-browsers'
    if os.path.isdir(root):
        for name in sorted(os.listdir(root)):
            path = os.path.join(root, name, 'chrome-linux', 'chrome')
            if os.path.exists(path):
                return path
    return None


class Client(object):
    """Один игрок: свой браузер, своя страница, свои счётчики ошибок."""

    def __init__(self, report, name, lane, port, shots_dir):
        self.report = report
        self.name = name
        self.lane = lane
        self.port = port
        self.shots_dir = shots_dir
        self.browser = None
        self.page = None
        self.console_errors = []
        self.page_errors = []
        self.warnings = []
        self.failed_requests = []

    # --- запуск и уборка ---------------------------------------------------

    def start(self, pw, executable):
        # Про флаги. swiftshader рисует на процессоре, поэтому три ключа
        # про GL обязательны — без них страница вообще не получит контекст.
        # А вот --disable-gpu-vsync здесь пробовать НЕ НАДО: кадров становится
        # вдвое больше, но поток рендера занимает процессор целиком, главный
        # поток встаёт на секунды, снапшоты приходят пачками по десять штук,
        # и клиент роняет уже не сотни шагов, а тысячи. Проверено.
        args = ['--no-sandbox', '--disable-dev-shm-usage',
                '--use-gl=angle', '--use-angle=swiftshader',
                '--enable-unsafe-swiftshader', '--mute-audio',
                '--autoplay-policy=no-user-gesture-required']
        try:
            self.browser = pw.chromium.launch(headless=True, args=args,
                                              executable_path=executable)
        except Exception as exc:
            raise TestBroken('браузер не запустился (%s): %s'
                             % (executable or 'путь по умолчанию', exc))
        context = self.browser.new_context(
            viewport={'width': BROWSER_WIDTH, 'height': BROWSER_HEIGHT})
        # Имя запоминается в localStorage (racing.name): так его подставит меню
        # ещё до первого hello. Поле ввода мы всё равно заполним руками.
        # Настройки графики — в пол: swiftshader рисует на процессоре, и на
        # среднем пресете клиент выдаёт 4 кадра в секунду, перестаёт успевать
        # за сервером (10 шагов на кадр — потолок main.js) и начинает ронять
        # шаги. Это поломка замера, а не игры, поэтому проверка идёт на низких
        # настройках: сеть и физика от них не зависят.
        settings = dict(LOW_GFX)
        settings['racing.name'] = self.name
        context.add_init_script(
            'try { %s } catch (e) {}'
            % ' '.join('localStorage.setItem(%s, %s);'
                       % (json.dumps(k), json.dumps(v))
                       for k, v in sorted(settings.items())))
        self.page = context.new_page()
        self.page.on('console', self._on_console)
        self.page.on('pageerror', self._on_page_error)
        self.page.on('requestfailed', self._on_request_failed)
        try:
            self.page.goto('http://127.0.0.1:%d/' % self.port,
                           wait_until='load', timeout=20000)
        except Exception as exc:
            raise TestBroken('страница не открылась: %s' % exc)

    def close(self):
        try:
            if self.browser is not None:
                self.browser.close()
        except Exception:
            pass
        self.browser = None
        self.page = None

    # --- сбор ошибок страницы ----------------------------------------------

    def _on_console(self, message):
        kind = message.type
        text = message.text
        if kind == 'error':
            self.console_errors.append(text)
        elif kind == 'warning':
            self.warnings.append(text)

    def _on_page_error(self, error):
        self.page_errors.append(str(error).split('\n')[0])

    def _on_request_failed(self, request):
        self.failed_requests.append('%s %s' % (request.method, request.url))

    # --- мостик в страницу --------------------------------------------------

    def js(self, expression, arg=None):
        return self.page.evaluate(expression, arg)

    def wait(self, expression, timeout=STAGE_TIMEOUT, arg=None):
        """Ждать истинности выражения. Возвращает True/False, не бросает."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                if self.page.evaluate(expression, arg):
                    return True
            except Exception:
                pass
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    def install_agent(self):
        answer = self.js(PAGE_AGENT, {'lane': self.lane})
        if answer != 'ok':
            raise TestBroken('агент не встал на страницу %s: %s' % (self.name, answer))

    def send(self, message):
        self.js('m => window.__racing.net.send(m)', message)

    def tap(self, code):
        self.js('c => window.__smoke.tap(c)', code)

    def smoke(self, path, default=None):
        """Поле window.__smoke по имени."""
        value = self.js('p => { const v = window.__smoke[p]; '
                        'return v === undefined ? null : v; }', path)
        return default if value is None else value

    def shot(self, tag):
        if not self.shots_dir:
            return
        path = os.path.join(self.shots_dir, '%s-%s.png' % (tag, self.name))
        try:
            self.page.screenshot(path=path)
        except Exception as exc:
            self.report.note('скриншот %s не вышел: %s' % (path, exc))


# ---------------------------------------------------------------------------
# Сценарий в браузере
# ---------------------------------------------------------------------------

def enter_name(client):
    """Имя игрока: набрать в поле меню (меню переподключится с ним само)."""
    typed = client.js(r"""(name) => {
        const root = document.getElementById('screen-menu');
        const input = root && root.querySelector('input[type="text"]');
        if (!input) return false;
        input.focus();
        input.value = name;
        input.dispatchEvent(new Event('input', { bubbles: true }));
        input.dispatchEvent(new Event('change', { bubbles: true }));
        input.blur();
        return true;
    }""", client.name)
    if not typed:
        # Поля не нашлось (меню перерисовали) — имя всё равно уедет из
        # localStorage при перезагрузке страницы.
        client.page.reload(wait_until='load')
        if not client.wait('() => !!window.__racing', 15.0):
            raise TestBroken('страница %s не загрузилась после перезапуска'
                             % client.name)
        client.install_agent()
        return False
    return True


def lobby_stage(report, host, guest):
    """Меню, комната, вход второго, машина и цвет, готовность."""
    report.stage('меню и лобби')

    for client in (host, guest):
        if not client.wait('() => window.__racing.net.connected', 10.0):
            report.fail('%s: клиент не подключился к серверу' % client.name)
            raise TestBroken('клиент не подключился, дальше идти некуда')
        client.install_agent()

    # Имя уезжает на сервер только в hello, поэтому menu.js после правки
    # имени тихо переподключается (main.js, onNameChange) — ждём, пока связь
    # вернётся уже с новым именем. Что его принял сервер, видно ниже,
    # в списке игроков комнаты.
    named = [enter_name(host), enter_name(guest)]
    propagated = [c.wait('n => window.__racing.net.connected '
                         '&& window.__racing.net.playerName === n', 10.0, c.name)
                  for c in (host, guest)]
    report.check(all(propagated), 'имя введено в меню: «%s» и «%s»%s'
                 % (host.name, guest.name,
                    '' if all(named) else ' (поля ввода в меню не нашлось, имя '
                                          'подставлено из localStorage)'))
    host.shot('01-menu')

    # Палитра: из welcome, пойманного после переподключения по смене имени.
    colors = None
    welcome = host.smoke('welcome')
    if isinstance(welcome, dict):
        colors = (welcome.get('content') or {}).get('colors')
    if not colors:
        try:
            sys.path.insert(0, BASE_DIR)
            from server import config as server_config
            colors = list(server_config.COLORS)
        except Exception:
            colors = ['#e5484d', '#0091ff']

    settings = {
        'track': RACE_TRACK,
        'laps': RACE_LAPS,
        'max_players': 4,
        'items_enabled': False,     # приёмке нужна предсказуемая гонка
        'collisions': True,
        'mirror': False,
    }
    host.send({'t': 'create_room', 'name': 'Приёмка', 'settings': settings})
    if not host.wait('() => window.__smoke.room && window.__smoke.room.id'):
        report.fail('комната не создалась: события room нет')
        raise TestBroken('без комнаты дальше идти некуда')
    room = host.smoke('room')
    room_id = room['id']

    guest.send({'t': 'list_rooms'})
    listed = guest.wait('id => { const r = window.__smoke.rooms; '
                        'return !!(r && r.rooms && r.rooms.some(x => x.id === id)); }',
                        STAGE_TIMEOUT, room_id)
    report.check(listed, 'комната видна в списке у второго игрока (id %s)' % room_id)

    guest.send({'t': 'join_room', 'room_id': room_id})
    joined = guest.wait('id => { const r = window.__smoke.room; '
                        'return !!(r && r.id === id); }', STAGE_TIMEOUT, room_id)
    both = host.wait('() => { const r = window.__smoke.room; '
                     'return !!(r && r.players && r.players.length === 2); }')
    if not (joined and both):
        report.fail('второй игрок не вошёл в комнату')
        raise TestBroken('в комнате не собралось двое')

    room = host.smoke('room')
    host_slot = room['you']
    guest_slot = guest.smoke('room')['you']
    owner_ok = room['you'] == room['owner_slot']
    names = sorted(p['name'] for p in room['players'])
    report.check(owner_ok and host_slot != guest_slot
                 and names == sorted([host.name, guest.name]),
                 'в комнате двое: слоты %d и %d, имена %s, владелец — хозяин'
                 % (host_slot, guest_slot, ', '.join(names)))

    # Экран лобби действительно показан (index.html, корни экранов).
    screens = all(c.js("() => !document.getElementById('screen-lobby').hidden "
                       "&& window.__racing.app.screen === 1")
                  for c in (host, guest))
    report.check(screens, 'у обоих открыт экран лобби')

    host.send({'t': 'set_car', 'car_id': HOST_CAR, 'color': colors[0]})
    guest.send({'t': 'set_car', 'car_id': GUEST_CAR,
                'color': colors[1] if len(colors) > 1 else colors[0]})

    def car_of(client, slot):
        room = client.smoke('room') or {}
        for player in room.get('players', []):
            if player['slot'] == slot:
                return player.get('car'), player.get('color')
        return None, None

    applied = host.wait(
        's => { const r = window.__smoke.room; if (!r) return false; '
        'const a = r.players.find(p => p.slot === s[0]); '
        'const b = r.players.find(p => p.slot === s[1]); '
        'return !!(a && b && a.car === s[2] && b.car === s[3]); }',
        STAGE_TIMEOUT, [host_slot, guest_slot, HOST_CAR, GUEST_CAR])
    host_car = car_of(host, host_slot)
    guest_car = car_of(host, guest_slot)
    report.check(applied, 'машины и цвета выбраны: %s %s и %s %s'
                 % (host_car[0], host_car[1], guest_car[0], guest_car[1]))

    host.send({'t': 'set_ready', 'ready': True})
    guest.send({'t': 'set_ready', 'ready': True})
    ready = host.wait('() => { const r = window.__smoke.room; '
                      'return !!(r && r.players.length === 2 '
                      '&& r.players.every(p => p.ready)); }')
    report.check(ready, 'оба готовы')
    host.shot('02-lobby')
    return host_slot, guest_slot


def countdown_stage(report, host, guest):
    """Старт гонки и обратный отсчёт."""
    report.stage('старт и отсчёт')
    started = time.monotonic()
    host.send({'t': 'start_race'})

    init = all(c.wait('() => window.__racing.app.raceBuilt') for c in (host, guest))
    if not init:
        report.fail('гонка не собралась: race_init не дошёл или сцена не построилась')
        raise TestBroken('гонка не началась')

    host.shot('03-countdown')

    racing = all(c.wait('() => window.__racing.app.racing '
                        '&& !document.getElementById("screen-hud").hidden', 20.0)
                 for c in (host, guest))
    elapsed = time.monotonic() - started
    values = [row['v'] for row in host.smoke('countdown', [])]
    report.check(racing and values[:4] == [3, 2, 1, 0],
                 'отсчёт прошёл: %s, гонка началась за %.1f с'
                 % (values[:4] or 'событий нет', elapsed))
    for client in (host, guest):
        client.js('() => { window.__smoke.drive = true; }')
    return racing


def pause_stage(report, host, guest, guest_slot):
    """Пауза: ставит гость, снимает хозяин. Время гонки обязано замереть."""
    report.stage('пауза')
    read = ('() => { const n = window.__racing.net; const t = performance.now(); '
            'return { race: n.raceTime(t), lap: n.currentLapTime(t), '
            'x: n.state.x, z: n.state.z, paused: window.__racing.app.paused }; }')

    marked = time.monotonic()
    guest.tap('KeyP')

    stopped = all(c.wait('() => window.__racing.app.paused', 5.0) for c in (host, guest))
    delay = time.monotonic() - marked
    if not stopped:
        report.fail('пауза не встала у обоих за %.1f с (клавиша P)' % delay)
        return False
    # Событие pause у хозяина должно назвать инициатором гостя (схема §9).
    marks = host.smoke('pauses', [])
    last = marks[-1] if marks else {}
    report.check(last.get('by') == guest_slot and last.get('reason') == 'player',
                 'пауза встала у обоих за %.2f с, событие pause называет '
                 'инициатором слот %s, причина «%s», бюджет %.0f с'
                 % (delay, last.get('by'), last.get('reason'),
                    last.get('budget') or 0.0))
    host.shot('05-pause')

    # Отсчёт замеров — уже с паузы: между нажатием клавиши и остановкой
    # машина честно проезжает свои полметра, проверяем мы не это.
    before = {c.name: c.js(read) for c in (host, guest)}
    time.sleep(PAUSE_HOLD)
    during = {c.name: c.js(read) for c in (host, guest)}
    race_drift = max(abs(during[n]['race'] - before[n]['race']) for n in during)
    lap_drift = max(abs(during[n]['lap'] - before[n]['lap']) for n in during)
    move = max(math.hypot(during[n]['x'] - before[n]['x'],
                          during[n]['z'] - before[n]['z']) for n in during)
    report.check(race_drift <= PAUSE_DRIFT_MAX and lap_drift <= PAUSE_DRIFT_MAX,
                 'за %.1f с паузы время гонки сдвинулось на %.3f с, время круга — '
                 'на %.3f с (порог %.2f с)'
                 % (PAUSE_HOLD, race_drift, lap_drift, PAUSE_DRIFT_MAX))
    report.check(move <= 0.01,
                 'своя машина на паузе стоит: сдвиг %.4f м' % move)

    marked = time.monotonic()
    host.tap('KeyP')
    resuming = all(c.wait('() => { const p = window.__smoke.pauses; '
                          'return p.length && p[p.length - 1].phase === "resuming"; }',
                          5.0) for c in (host, guest))
    running = all(c.wait('() => !window.__racing.app.paused', 8.0)
                  for c in (host, guest))
    back = time.monotonic() - marked
    report.check(resuming and running,
                 'пауза снята вторым игроком: отсчёт и старт за %.1f с' % back)

    after = host.js(read)
    time.sleep(0.5)
    later = host.js(read)
    report.check(later['race'] > after['race'] + 0.2,
                 'после снятия паузы время гонки снова идёт (+%.2f с за 0,5 с)'
                 % (later['race'] - after['race']))
    return True


def race_stage(report, host, guest, host_slot, guest_slot):
    """Едем до финиша, посередине — проверка паузы."""
    report.stage('гонка')
    for client, other in ((host, guest_slot), (guest, host_slot)):
        client.js('s => { window.__smoke.other = s; }', other)

    started = time.monotonic()
    paused_done = False
    shot_done = False
    deadline = started + RACE_DEADLINE

    while time.monotonic() < deadline:
        if not shot_done and time.monotonic() - started > 3.0:
            host.shot('04-race')
            shot_done = True
        if not paused_done and time.monotonic() - started > DRIVE_BEFORE_PAUSE:
            pause_stage(report, host, guest, guest_slot)
            paused_done = True
            report.stage('гонка (продолжение)')
        if all(c.js('() => !!window.__smoke.results') for c in (host, guest)):
            break
        time.sleep(0.25)

    finished = all(c.js('() => !!window.__smoke.results') for c in (host, guest))
    lasted = time.monotonic() - started
    report.check(finished, 'гонка доехала до итогов за %.0f с' % lasted
                 if finished else 'гонка не закончилась за %.0f с: итогов нет'
                 % lasted)
    return finished


def results_stage(report, host, guest):
    """Итоговая таблица и возврат в лобби кнопкой «В лобби»."""
    report.stage('итоги')
    rows = (host.smoke('results') or {}).get('rows') or []
    filled = [r for r in rows if r.get('place') and not r.get('dnf')]
    report.check(len(rows) >= 2 and len(filled) >= 1,
                 'итоговая таблица: %d строк, финишировавших %d, лучший круг '
                 'первого %s' % (len(rows), len(filled),
                                 rows[0].get('best_lap') if rows else '—'))

    shown = all(c.wait('() => !document.getElementById("screen-results").hidden '
                       '&& window.__racing.app.screen === 3', 10.0)
                for c in (host, guest))
    report.check(shown, 'экран итогов показан у обоих')
    host.shot('06-results')

    clicked = []
    for client in (host, guest):
        clicked.append(client.js(r"""() => {
            const root = document.getElementById('screen-results');
            const buttons = root ? root.querySelectorAll('button') : [];
            for (const b of buttons) {
                if ((b.textContent || '').toLowerCase().indexOf('лоб') >= 0) {
                    b.click();
                    return true;
                }
            }
            return false;
        }"""))
    back = all(c.wait('() => window.__racing.app.screen === 1 '
                      '&& !document.getElementById("screen-lobby").hidden', 10.0)
               for c in (host, guest))
    report.check(all(clicked) and back,
                 'кнопка «В лобби» вернула обоих в лобби (без выхода из комнаты)')
    host.shot('07-lobby-back')


def measure_stage(report, host, guest, host_slot, guest_slot):
    """Разбор записанных замеров: движение, круг, сеть, плавность."""
    report.stage('замеры')
    dump = ('() => { const S = window.__smoke; return { lt: S.lt, lx: S.lx, '
            'lz: S.lz, vt: S.vt, vx: S.vx, vz: S.vz, vvx: S.vvx, '
            'vvz: S.vvz, vms: S.vms, '
            'snap: S.snapMs, frames: S.frames, both: S.both, seen: S.seen, '
            'maxSpeed: S.maxSpeed, keys: S.keys, events: S.events, '
            'errors: S.errors, at: S.at, ax: S.ax, az: S.az, rec: S.rec, '
            'progress: S.progress - S.progStart, '
            'series: S.progressSeries, '
            'best: window.__racing.net.bestLap, dropped: window.__racing.app.dropped, '
            'fps: S.lastMs > S.firstMs ? S.frames * 1000 / (S.lastMs - S.firstMs) : 0 }; }')
    data = {'host': host.js(dump), 'guest': guest.js(dump)}

    # --- машина едет -------------------------------------------------------
    for client, key in ((host, 'host'), (guest, 'guest')):
        d = data[key]
        series = d['series']
        # Откат назад бывает законным: бот выбирается из отбойника задним
        # ходом, и за секунду между замерами успевает проехать назад метров
        # десять. Ненормально — когда путь уезжает назад большим куском.
        rollback = max([series[i - 1] - series[i] for i in range(1, len(series))]
                       or [0.0])
        report.check(d['progress'] >= MIN_PROGRESS and d['maxSpeed'] >= MIN_TOP_SPEED
                     and rollback <= MAX_ROLLBACK,
                     '%s: путь по трассе %.0f м, худший откат назад %.1f м '
                     '(порог %.0f), максимальная скорость %.1f м/с (%.0f км/ч), '
                     'клавишных событий %d'
                     % (client.name, d['progress'], rollback, MAX_ROLLBACK,
                        d['maxSpeed'], d['maxSpeed'] * 3.6, d['keys']))
        report.info('%s: кадров %d, %.1f кадра/с, пропущено шагов %d'
                    % (client.name, d['frames'], d['fps'], d['dropped']))

    # --- круг --------------------------------------------------------------
    for client, key, slot in ((host, 'host', host_slot), (guest, 'guest', guest_slot)):
        laps = [e for e in data[key]['events']
                if e.get('kind') == 'lap' and e.get('slot') == slot]
        best = data[key]['best']
        good = bool(laps) and laps[0].get('time', 0) > 0 and best > 0
        report.check(good, '%s: круг засчитан — событий lap %d, время круга %.2f с, '
                     'лучший круг %.2f с'
                     % (client.name, len(laps),
                        laps[0].get('time', 0.0) if laps else 0.0, best))

    # --- видимость чужой машины --------------------------------------------
    for client, key in ((host, 'host'), (guest, 'guest')):
        d = data[key]
        share = (d['seen'] / d['both']) if d['both'] else 0.0
        report.check(share >= 0.98 and len(d['vt']) > 100,
                     '%s видит чужую машину в %.0f %% кадров, пока та в гонке '
                     '(%d из %d, записано %d позиций)'
                     % (client.name, share * 100.0, d['seen'], d['both'],
                        len(d['vt'])))

    # --- снапшоты ----------------------------------------------------------
    rates = {}
    for client, key in ((host, 'host'), (guest, 'guest')):
        rates[key] = snapshot_hz(data[key]['snap'])
    good = all(SNAPSHOT_HZ_MIN <= rate <= SNAPSHOT_HZ_MAX for rate in rates.values())
    report.check(good, 'снапшоты: %s %.1f Гц, %s %.1f Гц (ожидание 20 Гц)'
                 % (host.name, rates['host'], guest.name, rates['guest']))

    # --- сходимость позиций ------------------------------------------------
    # Где гость предсказал себя против того, где его рисует хозяин.
    # Сравнение выровнено по времени: у хозяина чужая машина показана
    # на момент now - INTERP_DELAY, и этот момент записан вместе с позицией.
    # Половина первая: гость против сервера — верно ли он предсказывает себя.
    # Доля снапшотов сверх RECONCILE_EPS — вторая цифра из 10.2: сверка
    # по tick давала там 15 %, сверка по ack_seq (та самая ошибка) — 53 %.
    for client, key in ((host, 'host'), (guest, 'guest')):
        rec = data[key]['rec']
        if len(rec) < 50:
            report.fail('%s: расхождение предсказания мерить не по чему, '
                        'замеров %d' % (client.name, len(rec)))
            continue
        median = percentile(rec, 0.5)
        p95 = percentile(rec, 0.95)
        # p99 порогом не служит (хвост короткий и шумный), но печатается:
        # им меряют физику Rapier, где расхождение живёт именно в хвосте.
        p99 = percentile(rec, 0.99)
        eps = PREDICT_EPS_RAPIER if PHYSICS == 'rapier' else PREDICT_EPS
        over = sum(1 for v in rec if v > eps) / len(rec)
        report.check(median <= PREDICT_MEDIAN_MAX and p95 <= PREDICT_P95_MAX
                     and over <= PREDICT_OVER_EPS_MAX,
                     '%s предсказывает себя верно: расхождение с авторитетом '
                     'медиана %.3f м, p95 %.3f м, p99 %.3f м, сверх %.2f м — '
                     '%.0f %% из %d снапшотов (пороги %.2f м, %.2f м, %.0f %%)'
                     % (client.name, median, p95, p99, eps, over * 100.0, len(rec),
                        PREDICT_MEDIAN_MAX, PREDICT_P95_MAX,
                        PREDICT_OVER_EPS_MAX * 100.0))

    # Половина вторая: хозяин против сервера — там ли он рисует гостя.
    guest_data = data['guest']
    seen = data['host']
    deltas = []
    for i in range(len(seen['vt'])):
        point = interp_track(guest_data['at'], guest_data['ax'], guest_data['az'],
                             seen['vt'][i])
        if point is None:
            continue
        deltas.append(math.hypot(seen['vx'][i] - point[0], seen['vz'][i] - point[1]))
    if len(deltas) < 100:
        report.fail('сходимость позиций: сравнивать нечего, всего %d пар'
                    % len(deltas))
    else:
        median = percentile(deltas, 0.5)
        p95 = percentile(deltas, 0.95)
        report.check(median <= DIVERGE_MEDIAN_MAX and p95 <= DIVERGE_P95_MAX,
                     'позиции сходятся: хозяин рисует гостя в %.3f м от того '
                     'места, где тот был по серверу (медиана; p95 %.3f м, '
                     '%d пар, пороги %.2f и %.2f м)'
                     % (median, p95, len(deltas), DIVERGE_MEDIAN_MAX,
                        DIVERGE_P95_MAX))

    # То же самое, но в лоб: показанная гостю собственная машина против
    # показанной хозяину чужой. Без порога — на медленном стенде сюда попадают
    # пропущенные шаги главного цикла, — но число полезное: это ровно то
    # расхождение, которое увидели бы два игрока, глядя на свои экраны.
    direct = []
    for i in range(len(seen['vt'])):
        point = interp_track(guest_data['lt'], guest_data['lx'], guest_data['lz'],
                             seen['vt'][i])
        if point is not None:
            direct.append(math.hypot(seen['vx'][i] - point[0],
                                     seen['vz'][i] - point[1]))
    if direct:
        report.info('то же в лоб — предсказание гостя против картинки хозяина: '
                    'медиана %.3f м, p95 %.3f м (порога нет: сюда попадают '
                    'просадки стенда, %d пропущенных шагов за гонку)'
                    % (percentile(direct, 0.5), percentile(direct, 0.95),
                       guest_data['dropped']))

    # --- плавность чужой машины --------------------------------------------
    for client, key in ((host, 'host'), (guest, 'guest')):
        d = data[key]
        devs = speed_deviation(d['vt'], d['vx'], d['vz'], d['vvx'], d['vvz'],
                               d['vms'])
        if len(devs) < 100:
            report.fail('%s: плавность считать не по чему, кадров %d'
                        % (client.name, len(devs)))
            continue
        jumpy = sum(1 for v in devs if v > 0.5) / len(devs)
        p95 = percentile(devs, 0.95)
        report.check(jumpy <= SMOOTH_JUMPY_MAX and p95 <= SMOOTH_P95_MAX,
                     '%s: чужая машина едет гладко — отклонение видимой скорости '
                     'от заявленной p95 %.0f %% (порог %.0f %%), кадров хуже '
                     'половины %.1f %% (порог %.0f %%), всего %d кадров'
                     % (client.name, p95 * 100.0, SMOOTH_P95_MAX * 100.0,
                        jumpy * 100.0, SMOOTH_JUMPY_MAX * 100.0, len(devs)))
        shift = smoothness(d['vt'], d['vx'], d['vz'], d['vms'])
        if shift:
            report.info('%s: отклонение от гладкой траектории p95 %.4f м, пик '
                        '%.3f м при %.0f кадрах/с (12.12 мерил то же при 60)'
                        % (client.name, percentile(shift, 0.95), max(shift),
                           d['fps']))

    # --- ошибки протокола --------------------------------------------------
    problems = []
    for client, key in ((host, 'host'), (guest, 'guest')):
        for err in data[key]['errors']:
            problems.append('%s: %s/%s' % (client.name, err.get('code'),
                                           err.get('message')))
    report.check(not problems, 'событий error от сервера: %d%s'
                 % (len(problems), ('; ' + '; '.join(problems[:3])) if problems else ''))


def console_stage(report, clients):
    """Самая ценная проверка: ошибки в консоли и исключения страницы."""
    report.stage('консоль браузера')
    errors = 0
    for client in clients:
        errors += len(client.console_errors) + len(client.page_errors)
    for client in clients:
        for text in client.console_errors[:5]:
            report.info('%s: console.error: %s' % (client.name, text[:200]))
        for text in client.page_errors[:5]:
            report.info('%s: исключение страницы: %s' % (client.name, text[:200]))
        for text in client.warnings[:5]:
            report.note('%s: console.warn: %s' % (client.name, text[:200]))
        for text in client.failed_requests[:5]:
            report.note('%s: запрос не удался: %s' % (client.name, text[:200]))
    report.check(errors == 0,
                 'ошибок в консоли и исключений страницы: %d (у %s — %d и %d, '
                 'у %s — %d и %d); предупреждений %d'
                 % (errors,
                    clients[0].name, len(clients[0].console_errors),
                    len(clients[0].page_errors),
                    clients[1].name, len(clients[1].console_errors),
                    len(clients[1].page_errors),
                    sum(len(c.warnings) for c in clients)))


def run_browser(report, port, shots_dir):
    """Полный сценарий в двух браузерах. Возвращает False, если пропущен."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        report.skip('playwright не установлен, проверка браузера пропущена '
                    '(это инструмент разработки; серверная часть прогнана)')
        return False

    executable = find_chromium()
    if executable is None:
        report.note('предустановленный chromium не найден, пробуем штатный путь '
                    'playwright')

    host = Client(report, 'Хозяин', -0.35, port, shots_dir)
    guest = Client(report, 'Гость', 0.35, port, shots_dir)
    with sync_playwright() as pw:
        try:
            host.start(pw, executable)
            guest.start(pw, executable)
            report.ok('два headless-браузера открыли страницу игры')
            host_slot, guest_slot = lobby_stage(report, host, guest)
            if countdown_stage(report, host, guest):
                race_stage(report, host, guest, host_slot, guest_slot)
                measure_stage(report, host, guest, host_slot, guest_slot)
                results_stage(report, host, guest)
            console_stage(report, [host, guest])
        finally:
            host.close()
            guest.close()
    return True


# ---------------------------------------------------------------------------
# Серверная проверка без браузера: два websocket-клиента Tornado
# ---------------------------------------------------------------------------
#
# Нужна там, где playwright не установлен (то есть на всех игровых машинах):
# браузерная часть пропускается, но сервер, протокол и симуляция всё равно
# должны отзываться. Сценарий короче браузерного: лобби, старт, отсчёт,
# несколько секунд езды, пауза. Круг не наматывается — это минута времени
# ради того, что уже проверяет tools/test_sim.py.

SNAP_HEAD = struct.Struct('<BIIB')
SNAP_CAR = struct.Struct('<BBfffffBBbB')
BTN_THROTTLE = 1 << 0
BTN_BRAKE = 1 << 1
BTN_LEFT = 1 << 2
BTN_RIGHT = 1 << 3
INPUT_PACK = struct.Struct('<BIBB')


def decode_snapshot(data):
    """Разбор бинарного снапшота (раздел 5.3). Нужны только машины."""
    if len(data) < SNAP_HEAD.size or data[0] != 0x10:
        return None
    kind, tick, ack, count = SNAP_HEAD.unpack_from(data, 0)
    cars = {}
    offset = SNAP_HEAD.size
    for _ in range(count):
        if offset + SNAP_CAR.size > len(data):
            return None
        slot, flags, x, z, yaw, vx, vz, lap, place, steer, drift = \
            SNAP_CAR.unpack_from(data, offset)
        offset += SNAP_CAR.size
        cars[slot] = {'flags': flags, 'x': x, 'z': z, 'yaw': yaw,
                      'vx': vx, 'vz': vz, 'lap': lap, 'place': place}
    return {'tick': tick, 'ack': ack, 'cars': cars}


class WsClient(object):
    """Игрок без браузера: тот же протокол, тот же бинарный ввод."""

    def __init__(self, name, port):
        self.name = name
        self.url = 'ws://127.0.0.1:%d/ws' % port
        self.conn = None
        self.reader = None
        self.alive = True
        self.welcome = None
        self.room = None
        self.rooms = None
        self.race_init = None
        self.countdown = []
        self.pauses = []
        self.events = []
        self.results = None
        self.errors = []
        self.snap = None
        self.snap_times = []
        self.seq = 0

    async def connect(self):
        import tornado.websocket
        self.conn = await tornado.websocket.websocket_connect(self.url)
        self.reader = asyncio.ensure_future(self._read_loop())

    async def _read_loop(self):
        while self.alive:
            message = await self.conn.read_message()
            if message is None:
                self.alive = False
                return
            if isinstance(message, bytes):
                snap = decode_snapshot(message)
                if snap is not None:
                    self.snap = snap
                    self.snap_times.append(time.monotonic() * 1000.0)
                continue
            try:
                msg = json.loads(message)
            except ValueError:
                continue
            kind = msg.get('t')
            if kind == 'welcome':
                self.welcome = msg
            elif kind == 'rooms':
                self.rooms = msg
            elif kind == 'room':
                self.room = msg
            elif kind == 'race_init':
                self.race_init = msg
            elif kind == 'countdown':
                self.countdown.append(msg['value'])
            elif kind == 'race_event':
                self.events.append(msg)
            elif kind == 'results':
                self.results = msg
            elif kind == 'pause':
                self.pauses.append(msg)
            elif kind == 'error':
                self.errors.append(msg)
            elif kind == 'ping':
                self.send({'t': 'pong', 't0': msg.get('t0')})

    def send(self, message):
        self.conn.write_message(json.dumps(message))

    def send_input(self, buttons):
        """ВНИМАНИЕ: только бинарным кадром, иначе сервер рвёт связь (§5)."""
        self.seq += 1
        self.conn.write_message(
            INPUT_PACK.pack(0x01, self.seq, buttons, 0), binary=True)

    def close(self):
        self.alive = False
        try:
            if self.conn is not None:
                self.conn.close()
        except Exception:
            pass


async def ws_wait(check, timeout=STAGE_TIMEOUT):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        await asyncio.sleep(0.05)
    return False


class WsBot(object):
    """Тот же закон вождения, что у бота в браузере, только на массивах JSON."""

    def __init__(self, track, stats, lane):
        self.t = track
        self.lane = lane
        self.n = track['count']
        self.step = track['sample_step']
        self.hint = 0
        top = self._top_speed(stats)
        self.top = top
        self.lat = stats['grip_step'] * 160.0 * 0.72
        self.brake = stats['brake_force'] * 0.70
        self.radii = self._radii()

    @staticmethod
    def _top_speed(stats):
        a = stats['drag']
        b = stats['roll'] + stats['engine_force'] / stats['max_speed']
        c = -stats['engine_force']
        return (-b + math.sqrt(b * b - 4.0 * a * c)) / (2.0 * a)

    def _radii(self):
        x, z, n = self.t['x'], self.t['z'], self.n
        out = [0.0] * n
        for i in range(n):
            a, b, c = (i - 2) % n, i, (i + 2) % n
            ab = math.hypot(x[b] - x[a], z[b] - z[a])
            bc = math.hypot(x[c] - x[b], z[c] - z[b])
            ca = math.hypot(x[a] - x[c], z[a] - z[c])
            area2 = abs((x[b] - x[a]) * (z[c] - z[a]) - (x[c] - x[a]) * (z[b] - z[a]))
            out[i] = 1e6 if area2 < 1e-9 else ab * bc * ca / (2.0 * area2)
        return out

    def nearest(self, px, pz):
        x, z, n = self.t['x'], self.t['z'], self.n
        best, best_d = self.hint, 1e30
        for off in range(-24, 25):
            j = (self.hint + off) % n
            d = (px - x[j]) ** 2 + (pz - z[j]) ** 2
            if d < best_d:
                best_d, best = d, j
        if best_d > 576.0:                 # подсказка потеряна — грубый проход
            for j in range(0, n, 4):
                d = (px - x[j]) ** 2 + (pz - z[j]) ** 2
                if d < best_d:
                    best_d, best = d, j
        self.hint = best
        return best

    def drive(self, car):
        t = self.t
        n, step = self.n, self.step
        speed = math.hypot(car['vx'], car['vz'])
        idx = self.nearest(car['x'], car['z'])
        look = 7.0 + speed * 0.55
        j = (idx + int(round(look / step))) % n
        tx = t['x'][j] + t['nx'][j] * self.lane * t['hw'][j]
        tz = t['z'][j] + t['nz'][j] * self.lane * t['hw'][j]
        err = math.atan2(tx - car['x'], tz - car['z']) - car['yaw']
        while err > math.pi:
            err -= math.pi * 2.0
        while err < -math.pi:
            err += math.pi * 2.0

        limit = self.top
        reach = speed * speed / (2.0 * self.brake) + 24.0
        dist = 8.0
        while dist <= reach:
            k = (idx + int(dist / step)) % n
            corner = math.sqrt(self.lat * (self.radii[k] + t['hw'][k]))
            allow = math.sqrt(corner * corner + 2.0 * self.brake * dist)
            if allow < limit:
                limit = allow
            dist += 8.0

        buttons = 0
        if speed < limit:
            buttons |= BTN_THROTTLE
        elif speed > limit * 1.12:
            buttons |= BTN_BRAKE
        if err > 0.030:
            buttons |= BTN_LEFT
        elif err < -0.030:
            buttons |= BTN_RIGHT
        return buttons


async def ws_scenario(report, port):
    """Лобби, старт, езда и пауза — без браузера."""
    host = WsClient('Хозяин', port)
    guest = WsClient('Гость', port)
    try:
        try:
            await host.connect()
            await guest.connect()
        except Exception as exc:
            raise TestBroken('websocket не подключился: %s' % exc)

        host.send({'t': 'hello', 'name': host.name})
        guest.send({'t': 'hello', 'name': guest.name})
        got = await ws_wait(lambda: host.welcome and guest.welcome, 10.0)
        if not got:
            report.fail('welcome не пришёл ни одному клиенту')
            return
        content = host.welcome['content']
        report.ok('welcome получен: трасс %d, машин %d, цветов %d'
                  % (len(content['tracks']), len(content['cars']),
                     len(content['colors'])))

        settings = {'track': RACE_TRACK, 'laps': RACE_LAPS, 'max_players': 4,
                    'items_enabled': False, 'collisions': True, 'mirror': False}
        host.send({'t': 'create_room', 'name': 'Приёмка', 'settings': settings})
        if not await ws_wait(lambda: host.room is not None):
            report.fail('комната не создалась')
            return
        room_id = host.room['id']
        guest.send({'t': 'list_rooms'})
        listed = await ws_wait(
            lambda: guest.rooms is not None
            and any(r['id'] == room_id for r in guest.rooms['rooms']))
        guest.send({'t': 'join_room', 'room_id': room_id})
        joined = await ws_wait(lambda: guest.room is not None
                               and guest.room['id'] == room_id)
        report.check(listed and joined,
                     'комната создана и второй игрок вошёл (id %s)' % room_id)

        colors = content['colors']
        cars = {c['id']: c for c in content['cars']}
        host.send({'t': 'set_car', 'car_id': HOST_CAR, 'color': colors[0]})
        guest.send({'t': 'set_car', 'car_id': GUEST_CAR, 'color': colors[1]})
        host.send({'t': 'set_ready', 'ready': True})
        guest.send({'t': 'set_ready', 'ready': True})
        ready = await ws_wait(lambda: host.room is not None
                              and len(host.room['players']) == 2
                              and all(p['ready'] for p in host.room['players']))
        report.check(ready, 'машины выбраны, оба готовы')

        host.send({'t': 'start_race'})
        inited = await ws_wait(lambda: host.race_init and guest.race_init, 10.0)
        if not inited:
            report.fail('race_init не пришёл')
            return
        running = await ws_wait(lambda: 0 in host.countdown, 10.0)
        report.check(running and host.countdown[:4] == [3, 2, 1, 0],
                     'отсчёт прошёл: %s' % host.countdown[:4])

        track = host.race_init['track']
        slots = {}
        for client in (host, guest):
            slots[client.name] = client.room['you']
        bots = {
            host.name: WsBot(track, cars[HOST_CAR]['stats'], -0.35),
            guest.name: WsBot(track, cars[GUEST_CAR]['stats'], 0.35),
        }

        start = time.monotonic()
        first = {}
        while time.monotonic() - start < 8.0:
            for client in (host, guest):
                snap = client.snap
                if snap is None:
                    continue
                car = snap['cars'].get(slots[client.name])
                if car is None:
                    continue
                first.setdefault(client.name, (car['x'], car['z']))
                client.send_input(bots[client.name].drive(car))
            await asyncio.sleep(1.0 / 60.0)

        moved = {}
        speeds = {}
        for client in (host, guest):
            car = client.snap['cars'][slots[client.name]]
            base = first.get(client.name, (car['x'], car['z']))
            moved[client.name] = math.hypot(car['x'] - base[0], car['z'] - base[1])
            speeds[client.name] = math.hypot(car['vx'], car['vz'])
        report.check(min(moved.values()) > 40.0 and min(speeds.values()) > 8.0,
                     'обе машины поехали: смещение %.0f и %.0f м, скорость '
                     '%.1f и %.1f м/с'
                     % (moved[host.name], moved[guest.name],
                        speeds[host.name], speeds[guest.name]))

        rate = snapshot_hz(host.snap_times)
        report.check(SNAPSHOT_HZ_MIN <= rate <= SNAPSHOT_HZ_MAX,
                     'снапшоты идут %.1f Гц (ожидание 20 Гц)' % rate)
        report.check(host.snap['ack'] > 0 and guest.snap['ack'] > 0,
                     'сервер принимает бинарный ввод: ack_seq %d и %d'
                     % (host.snap['ack'], guest.snap['ack']))
        report.check(len(host.snap['cars']) == 2,
                     'в снапшоте обе машины: %d' % len(host.snap['cars']))

        tick_before = host.snap['tick']
        guest.send({'t': 'set_pause', 'paused': True})
        paused = await ws_wait(
            lambda: host.pauses and host.pauses[-1]['phase'] == 'paused'
            and guest.pauses and guest.pauses[-1]['phase'] == 'paused', 5.0)
        await asyncio.sleep(PAUSE_HOLD)
        frozen = host.snap['tick']
        report.check(paused and frozen - tick_before < 60,
                     'пауза встала у обоих, тик сервера замер (было %d, стало %d)'
                     % (tick_before, frozen))
        host.send({'t': 'set_pause', 'paused': False})
        resumed = await ws_wait(
            lambda: host.pauses and host.pauses[-1]['phase'] == 'running', 8.0)
        report.check(resumed, 'пауза снята вторым игроком, гонка поехала')

        host.send({'t': 'leave_room'})
        guest.send({'t': 'leave_room'})
        await asyncio.sleep(0.3)
        report.check(not host.errors and not guest.errors,
                     'событий error от сервера: %d'
                     % (len(host.errors) + len(guest.errors)))
    finally:
        host.close()
        guest.close()


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog='smoke_test.py',
        description='Приёмочный прогон: сервер, два браузера, одна гонка.')
    parser.add_argument('--port', type=int, default=0,
                        help='порт сервера (по умолчанию свободный; 8000 '
                             'не занимаем — там может идти живая игра)')
    parser.add_argument('--verbose', action='store_true',
                        help='печатать подробности и вывод сервера')
    parser.add_argument('--keep-screenshots', metavar='DIR', default=None,
                        help='сохранить скриншоты экранов в каталог '
                             '(по умолчанию не сохраняются)')
    parser.add_argument('--no-browser', action='store_true',
                        help='не открывать браузер: только серверная проверка')
    parser.add_argument('--physics', default=None,
                        help='чем сервер считает гонку: classic (умолчание), '
                             'shadow или rapier. Ключ уезжает в run.py как '
                             'есть; при rapier бонусов в заезде нет (§12.24), '
                             'и проверки бонусов прогон пропускает')
    return parser.parse_args(argv)


def main(argv=None):
    global PHYSICS
    args = parse_args(argv)
    PHYSICS = args.physics or 'classic'
    report = Report(args.verbose)

    shots_dir = None
    if args.keep_screenshots:
        shots_dir = os.path.abspath(args.keep_screenshots)
        try:
            os.makedirs(shots_dir, exist_ok=True)
        except OSError as exc:
            print('Не смог создать каталог для скриншотов: %s' % exc, file=sys.stderr)
            return 2

    port = args.port or free_port()
    server = Server(port, args.verbose, args.physics)
    broken = None
    try:
        print('Приёмка: порт %d, трасса %s, кругов %d'
              % (port, RACE_TRACK, RACE_LAPS), flush=True)
        warn_if_busy()
        report.stage('сервер')
        started = time.monotonic()
        server.start()
        report.ok('сервер поднялся на порту %d за %.1f с'
                  % (port, time.monotonic() - started))
        check_static(report, port)

        with_browser = False
        if not args.no_browser:
            with_browser = run_browser(report, port, shots_dir)
        else:
            report.skip('браузер отключён ключом --no-browser')
        if not with_browser:
            report.stage('сервер без браузера')
            asyncio.run(ws_scenario(report, port))
    except GameBroken as exc:
        report.fail(str(exc))
        if args.verbose:
            report.info(server.tail(25))
    except TestBroken as exc:
        broken = str(exc)
    except KeyboardInterrupt:
        broken = 'прервано с клавиатуры'
    except Exception as exc:
        # Сюда попадает всё неожиданное — например, playwright, у которого
        # не дождалась страница. Трассировку показываем только с --verbose:
        # глазами читают вывод, а не стек.
        broken = '%s: %s' % (type(exc).__name__, exc)
        if args.verbose:
            traceback.print_exc()
    finally:
        # Лог сервера смотрим всегда, даже если прогон оборвался на середине:
        # исключение на той стороне — самая важная новость из всех.
        if server.lines:
            report.stage('сервер: итог')
            errors = server.errors()
            report.check(not errors, 'исключений в логе сервера: %d%s'
                         % (len(errors), ('; ' + errors[0][:160]) if errors else ''))
            if errors and args.verbose:
                for line in errors[:10]:
                    report.info(line[:200])
        if shots_dir:
            report.info('скриншоты: %s' % shots_dir)
        server.stop()

    return report.summary(broken)


if __name__ == '__main__':
    sys.exit(main())
