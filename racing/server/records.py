# -*- coding: utf-8 -*-
"""Рекорды кругов, которые живут между сессиями сервера.

Что хранится
------------
На каждую трассу — абсолютный рекорд круга и рекорд на каждую машину:

    трасса + машина -> время, имя, дата
    трасса          -> время, имя, машина, дата   (абсолютный)

Зеркальная трасса — это другая трасса: ключ у неё ``<id>@mirror``. Иначе
рекорд «Серпантина» побивался бы кругом по «Серпантину наоборот», а это
разные повороты.

Формат файла
------------
Обычный JSON с отступами и `ensure_ascii=False`: его можно открыть, прочитать
глазами и поправить руками — это прямое требование. Схема::

    {
      "version": 1,
      "tracks": {
        "serpentine": {
          "best":  {"time": 39.58, "name": "Вася", "car": "rocket",
                    "date": "2026-09-15 14:02"},
          "cars":  {"rocket": {"time": 39.58, "name": "Вася",
                               "date": "2026-09-15 14:02"}}
        }
      }
    }

Почему запись не блокирует игровой цикл
---------------------------------------
Комната и запись на диск живут в одном потоке IOLoop: синхронный ``write``
на медленной файловой системе останавливает ВСЮ комнату, а не только
рекорды. Поэтому:

* побитие рекорда правит только словарь в памяти (это доли микросекунды)
  и взводит отложенный сброс;
* сброс через ``RECORDS_FLUSH_DELAY`` секунд сериализует данные в строку
  (тоже в цикле, но это десятки микросекунд на десяток записей) и отдаёт
  саму запись в отдельный поток через ``run_in_executor``;
* пока поток пишет, новые рекорды спокойно копятся: по возвращении сброс
  повторяется, если данные снова изменились.

Запись атомарная: временный файл в том же каталоге, ``flush`` + ``fsync``,
затем ``os.replace``. Внезапное выключение машины оставит либо старый файл
целиком, либо новый целиком, но не половину.

Устойчивость
------------
Игра обязана стартовать, даже если рекордов нет или файл испорчен. Поэтому
загрузка не бросает наружу ничего: битый файл отодвигается в ``*.corrupt``
(чтобы его можно было посмотреть, но чтобы он не мешал писать новый),
а хранилище стартует пустым. Отключённое хранилище (``--no-records``)
ведёт себя как пустое и не трогает диск вообще.
"""

import json
import os
import time

from concurrent.futures import ThreadPoolExecutor

from tornado.ioloop import IOLoop

from . import config


def _now_stamp():
    """Дата рекорда в читаемом человеком виде (местное время)."""
    return time.strftime('%Y-%m-%d %H:%M')


def track_key(track_id, mirror=False):
    """Ключ раздела в файле: зеркальная трасса — отдельная трасса."""
    return '%s@mirror' % track_id if mirror else str(track_id)


def split_key(key):
    """Обратное к track_key: (track_id, mirror)."""
    if key.endswith('@mirror'):
        return key[:-len('@mirror')], True
    return key, False


def _clean_entry(raw, with_car):
    """Одна запись рекорда из файла. Мусор -> None, а не исключение."""
    if not isinstance(raw, dict):
        return None
    value = raw.get('time')
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    value = float(value)
    # Круг короче секунды или длиннее часа — это не круг, а порча файла.
    if not 1.0 <= value <= 3600.0:
        return None
    entry = {
        'time': round(value, 3),
        'name': str(raw.get('name') or '')[:config.NAME_MAX_LEN],
        'date': str(raw.get('date') or '')[:32],
    }
    if with_car:
        entry['car'] = str(raw.get('car') or '')[:64]
    return entry


class RecordStore(object):
    """Лучшие круги сервера: чтение при старте, запись вне игрового цикла."""

    def __init__(self, path, enabled=True, log=None):
        self.path = path
        self.enabled = bool(enabled) and bool(path)
        self._log = log or (lambda message: None)
        self._tracks = {}            # ключ трассы -> {'best':..., 'cars':{...}}
        self._dirty = False
        self._timer = None
        self._writing = False
        self._executor = None
        self.write_errors = 0
        self.loaded = False

    # --- загрузка ------------------------------------------------------------

    def load(self):
        """Прочитать файл. Наружу не бросает ничего: игра важнее рекордов."""
        if not self.enabled:
            self._log('рекорды отключены: ничего не читаем и не пишем')
            return self
        if not os.path.isfile(self.path):
            self._log('файла рекордов нет (%s) — начинаем с чистого листа'
                      % self.path)
            self.loaded = True
            return self
        try:
            with open(self.path, 'r', encoding='utf-8') as fp:
                data = json.load(fp)
        except Exception as exc:
            self._quarantine(exc)
            return self
        if not isinstance(data, dict):
            self._quarantine('в файле не объект, а %s' % type(data).__name__)
            return self
        tracks = data.get('tracks')
        if not isinstance(tracks, dict):
            self._quarantine('нет раздела "tracks"')
            return self
        self._tracks = self._clean_tracks(tracks)
        self.loaded = True
        self._log('рекорды прочитаны: %s, трасс %d'
                  % (self.path, len(self._tracks)))
        return self

    def _clean_tracks(self, tracks):
        """Просеять прочитанное: каждая битая запись выбрасывается поштучно."""
        out = {}
        for key, section in tracks.items():
            if len(out) >= config.RECORDS_MAX_TRACKS:
                break
            if not isinstance(key, str) or not key or not isinstance(section, dict):
                continue
            best = _clean_entry(section.get('best'), True)
            cars = {}
            raw_cars = section.get('cars')
            if isinstance(raw_cars, dict):
                for car_id, raw in raw_cars.items():
                    if not isinstance(car_id, str) or not car_id:
                        continue
                    entry = _clean_entry(raw, False)
                    if entry is not None:
                        cars[car_id] = entry
            # Абсолютный рекорд мог не пережить правку руками: восстановим
            # его из рекордов по машинам, раз они целы.
            if best is None and cars:
                car_id = min(cars, key=lambda cid: cars[cid]['time'])
                entry = cars[car_id]
                best = {'time': entry['time'], 'name': entry['name'],
                        'car': car_id, 'date': entry['date']}
            if best is None and not cars:
                continue
            out[key] = {'best': best, 'cars': cars}
        return out

    def _quarantine(self, reason):
        """Файл не прочитался: отодвинуть его и стартовать пустым."""
        self._log('файл рекордов не прочитался (%s) — откладываю в сторону, '
                  'игра стартует без рекордов' % reason)
        spare = self.path + '.corrupt'
        try:
            os.replace(self.path, spare)
            self._log('битый файл сохранён как %s' % spare)
        except Exception as exc:
            # Даже это не повод падать: просто не будем перезаписывать файл.
            self._log('отложить битый файл не вышло (%s) — рекорды только '
                      'в памяти до перезапуска' % exc)
            self.enabled = False
        self._tracks = {}
        self.loaded = True

    # --- чтение --------------------------------------------------------------

    def best(self, track_id, mirror=False):
        """Абсолютный рекорд трассы или None."""
        section = self._tracks.get(track_key(track_id, mirror))
        return dict(section['best']) if section and section['best'] else None

    def table(self):
        """Таблица рекордов для клиента (едет в событии `rooms`).

        Список разделов, в каждом — абсолютный рекорд и рекорды по машинам.
        Порядок машин — по времени, чтобы клиенту не пришлось сортировать.
        """
        out = []
        for key in sorted(self._tracks):
            section = self._tracks[key]
            track_id, mirror = split_key(key)
            cars = [
                {'car': car_id, 'time': entry['time'],
                 'name': entry['name'], 'date': entry['date']}
                for car_id, entry in section['cars'].items()
            ]
            cars.sort(key=lambda row: row['time'])
            out.append({
                'track': track_id,
                'mirror': mirror,
                'best': dict(section['best']) if section['best'] else None,
                'cars': cars,
            })
        return out

    # --- запись --------------------------------------------------------------

    def submit(self, track_id, mirror, car_id, name, lap_time):
        """Учесть круг. Возвращает описание побития или None.

        Зовётся из игрового цикла, поэтому делает только правку словаря
        и взвод таймера: ни байта на диск здесь не уходит.
        """
        if not self.enabled:
            return None
        if not isinstance(lap_time, (int, float)) or isinstance(lap_time, bool):
            return None
        lap_time = round(float(lap_time), 3)
        if not 1.0 <= lap_time <= 3600.0:
            return None
        if not track_id or not isinstance(track_id, str):
            return None
        car_id = car_id if isinstance(car_id, str) and car_id else '?'
        name = (name or '')[:config.NAME_MAX_LEN]

        key = track_key(track_id, mirror)
        section = self._tracks.get(key)
        if section is None:
            if len(self._tracks) >= config.RECORDS_MAX_TRACKS:
                return None
            section = {'best': None, 'cars': {}}
            self._tracks[key] = section

        stamp = _now_stamp()
        prev_car = section['cars'].get(car_id)
        prev_best = section['best']
        beat_car = prev_car is None or lap_time < prev_car['time']
        beat_best = prev_best is None or lap_time < prev_best['time']
        if not beat_car and not beat_best:
            return None

        if beat_car:
            section['cars'][car_id] = {'time': lap_time, 'name': name, 'date': stamp}
        if beat_best:
            section['best'] = {'time': lap_time, 'name': name,
                               'car': car_id, 'date': stamp}
        self._touch()

        # Событие в комнате нужно одно, поэтому сообщаем о сильнейшем из двух.
        if beat_best:
            return {
                'scope': 'track', 'track': track_id, 'mirror': bool(mirror),
                'car': car_id, 'name': name, 'time': lap_time,
                'prev': prev_best['time'] if prev_best else None,
                'prev_name': prev_best['name'] if prev_best else None,
                'first': prev_best is None,
            }
        return {
            'scope': 'car', 'track': track_id, 'mirror': bool(mirror),
            'car': car_id, 'name': name, 'time': lap_time,
            'prev': prev_car['time'] if prev_car else None,
            'prev_name': prev_car['name'] if prev_car else None,
            'first': prev_car is None,
        }

    def _touch(self):
        """Пометить данные изменёнными и отложить сброс на диск."""
        self._dirty = True
        if self._timer is not None or self._writing:
            return
        self._timer = IOLoop.current().call_later(config.RECORDS_FLUSH_DELAY,
                                                  self._flush)

    def _serialize(self):
        """JSON-текст файла. Дёшево: записей десятки, не тысячи."""
        return json.dumps(
            {'version': config.RECORDS_VERSION, 'tracks': self._tracks},
            ensure_ascii=False, indent=2, sort_keys=True) + '\n'

    def _flush(self):
        """Отложенный сброс: сериализуем здесь, пишем в отдельном потоке."""
        self._timer = None
        if not self.enabled or not self._dirty or self._writing:
            return
        self._dirty = False
        self._writing = True
        text = self._serialize()
        try:
            loop = IOLoop.current()
            future = loop.run_in_executor(self._pool(), _write_atomic,
                                          self.path, text)
        except Exception as exc:
            # Потока не досталось — пишем прямо здесь. Это хуже (комната
            # встанет на время записи), но потерять рекорды хуже вдвойне.
            self._writing = False
            self._log('не удалось отдать запись рекордов потоку (%s), '
                      'пишу в цикле' % exc)
            self._write_now(text)
            return
        loop.add_future(future, self._after_write)

    def _after_write(self, future):
        self._writing = False
        try:
            future.result()
        except Exception as exc:
            self.write_errors += 1
            self._log('рекорды не записались (%s): %s' % (self.path, exc))
        if self._dirty:
            self._touch()

    def _write_now(self, text):
        try:
            _write_atomic(self.path, text)
        except Exception as exc:
            self.write_errors += 1
            self._log('рекорды не записались (%s): %s' % (self.path, exc))

    def _pool(self):
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1,
                                                thread_name_prefix='records')
        return self._executor

    def flush_now(self):
        """Синхронная запись: только на остановке сервера, цикл уже не нужен."""
        if self._timer is not None:
            try:
                IOLoop.current().remove_timeout(self._timer)
            except Exception:
                pass
            self._timer = None
        if self.enabled and self._dirty:
            self._dirty = False
            self._write_now(self._serialize())

    def close(self):
        """Дописать несохранённое и погасить поток."""
        self.flush_now()
        if self._executor is not None:
            self._executor.shutdown(wait=True)
            self._executor = None

    def __repr__(self):
        return '<RecordStore %s трасс=%d%s>' % (
            self.path, len(self._tracks), '' if self.enabled else ' ВЫКЛ')


def _write_atomic(path, text):
    """Записать файл атомарно: временный рядом, fsync, переименование.

    Выполняется в отдельном потоке, поэтому не трогает ничего, кроме
    переданных аргументов и файловой системы.
    """
    directory = os.path.dirname(os.path.abspath(path)) or '.'
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(directory, '.%s.tmp' % os.path.basename(path))
    with open(tmp, 'w', encoding='utf-8') as fp:
        fp.write(text)
        fp.flush()
        os.fsync(fp.fileno())
    os.replace(tmp, path)
