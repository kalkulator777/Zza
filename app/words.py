"""Банк слов для Угадайки."""

import json
import logging
import os
import random
import re

log = logging.getLogger("words")

WORDS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "data", "words.json")

# Запасной набор на случай, если файла банка нет: играть можно, но скучно.
FALLBACK = [
    ("кот", 1), ("дом", 1), ("солнце", 1), ("дерево", 1), ("рыба", 1), ("книга", 1),
    ("часы", 1), ("гриб", 1), ("ключ", 1), ("лампочка", 1), ("чайник", 1), ("очки", 1),
    ("зонт", 1), ("мяч", 1), ("ёжик", 1), ("снеговик", 2), ("маяк", 2), ("кактус", 2),
    ("гитара", 2), ("вертолёт", 2), ("пингвин", 2), ("мороженое", 2), ("робот", 2),
    ("замок", 2), ("пирамида", 2), ("осьминог", 2), ("велосипед", 2), ("телескоп", 3),
    ("эскалатор", 3), ("маскарад", 3), ("водопад", 2), ("паровоз", 2), ("скелет", 2),
    ("подсолнух", 2), ("будильник", 2), ("аквариум", 2), ("парашют", 2), ("карусель", 3),
]

_PUNCT = re.compile(r"[^\w\s-]", re.UNICODE)
_SPACES = re.compile(r"\s+")

_bank = None


def normalize(text):
    """Приводим догадку и слово к одному виду: регистр, ё, пунктуация, пробелы."""
    text = (text or "").lower().replace("ё", "е").replace("-", " ")
    text = _PUNCT.sub("", text)
    return _SPACES.sub(" ", text).strip()


def load():
    global _bank
    if _bank is not None:
        return _bank
    words = []
    try:
        with open(WORDS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        for item in data.get("words", []):
            word = str(item.get("w", "")).strip()
            if not word:
                continue
            level = item.get("lvl", 2)
            level = level if level in (1, 2, 3) else 2
            words.append((word, level))
        log.info("Банк слов: %d слов из %s", len(words), WORDS_FILE)
    except (OSError, ValueError) as exc:
        log.warning("Банк слов не прочитан (%s), беру запасной набор", exc)
    if not words:
        words = list(FALLBACK)
    _bank = words
    return _bank


def parse_custom(text):
    """Свои слова: по одному в строке или через запятую."""
    raw = re.split(r"[,\n;]+", text or "")
    return [w.strip() for w in raw if 1 < len(w.strip()) <= 40]


def pick(count, difficulty="mixed", custom_words="", used=()):
    """Выдать count разных слов, стараясь не повторять уже сыгранные."""
    custom = parse_custom(custom_words)
    if custom:
        pool = [(w, 2) for w in custom]
    else:
        levels = {"easy": (1, 2), "hard": (2, 3), "mixed": (1, 2, 3)}.get(difficulty, (1, 2, 3))
        pool = [(w, l) for w, l in load() if l in levels] or load()

    used_norm = {normalize(w) for w in used}
    fresh = [w for w, _ in pool if normalize(w) not in used_norm]
    if len(fresh) < count:
        fresh = [w for w, _ in pool]
    random.shuffle(fresh)
    return fresh[:count]


def distance(a, b):
    """Расстояние Левенштейна — чтобы отличать опечатку от неверной догадки."""
    if a == b:
        return 0
    if abs(len(a) - len(b)) > 2:
        return 3
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def is_close(guess, word):
    """Догадка «почти»: одна опечатка в достаточно длинном слове."""
    if len(word) < 5 or not guess:
        return False
    return distance(guess, word) == 1
