#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Собирает площадку катания в ОДИН html, который открывается двойным щелчком.

Зачем: модульные скрипты и fetch из file:// браузер запрещает (origin null),
поэтому «просто открыть файл» работает только если внутри уже лежит всё —
и three.js, и .wasm. Отдельные файлы остаются для разработки, этот — для того,
чтобы отдать страницу в руки и не объяснять, как поднять сервер.

    python3 native/testbed/build_standalone.py [куда.html]

По умолчанию кладёт рядом с собой в testbed_standalone.html.
Файл получается крупным (около 1,9 МБ) — это цена самодостаточности.
"""
import base64
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.normpath(os.path.join(HERE, '..', '..', 'static', 'vendor'))
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, 'testbed_standalone.html')

IMPORT_RE = re.compile(r'import\{([^}]*)\}from["\'][^"\']+["\'];?')
EXPORT_RE = re.compile(r'\bexport\{([^}]*)\}\s*(?:from\s*["\']([^"\']+)["\'])?;?')


def parse_list(text):
    """`a as B, c` -> [('a', 'B'), ('c', 'c')]"""
    out = []
    for item in text.split(','):
        item = item.strip()
        if not item:
            continue
        parts = item.split(' as ')
        if len(parts) == 2:
            out.append((parts[0].strip(), parts[1].strip()))
        else:
            out.append((item, item))
    return out


def to_iife(src, core_var=None):
    """Модуль ES -> обычная функция, возвращающая объект экспортов.

    Экспортов в файле может быть несколько (three.module.min.js сначала
    перепубликовывает имена ядра через `export{...}from"./three.core.min.js"`,
    а список своих отдаёт в конце), поэтому каждый `export{...}` превращается
    в дописывание в общий объект, а форма с `from` берёт имена прямо из ядра.
    """
    m = IMPORT_RE.search(src)
    if m:
        if core_var is None:
            raise SystemExit('модуль импортирует, а подставить нечего')
        pairs = parse_list(m.group(1))
        binding = 'const {' + ','.join('%s:%s' % (a, b) for a, b in pairs) + \
                  '} = ' + core_var + ';'
        src = src[:m.start()] + binding + src[m.end():]

    found = list(EXPORT_RE.finditer(src))
    if not found:
        raise SystemExit('в модуле не нашёлся список экспортов')
    out = []
    last = 0
    for m in found:
        pairs = parse_list(m.group(1))
        out.append(src[last:m.start()])
        if m.group(2):
            # перепубликация чужих имён: берём их из уже собранного ядра
            if core_var is None:
                raise SystemExit('перепубликация из модуля, которого нет')
            body = ','.join('%s:%s.%s' % (b, core_var, a) for a, b in pairs)
        else:
            body = ','.join('%s:%s' % (b, a) for a, b in pairs)
        out.append('Object.assign(__EX__,{' + body + '});')
        last = m.end()
    out.append(src[last:])
    body = ''.join(out)
    return '(function(){\nconst __EX__={};\n' + body + '\nreturn __EX__;\n})()'


def main():
    for name in ('three.core.min.js', 'three.module.min.js'):
        if not os.path.exists(os.path.join(VENDOR, name)):
            print('не нашёл %s в %s' % (name, VENDOR))
            return 1
    wasm_path = os.path.join(HERE, 'racing_physics.wasm')
    if not os.path.exists(wasm_path):
        print('нет racing_physics.wasm — собери крейт:')
        print('  cd native && cargo build --release --target wasm32-unknown-unknown')
        return 1

    core = open(os.path.join(VENDOR, 'three.core.min.js'), encoding='utf-8').read()
    main_js = open(os.path.join(VENDOR, 'three.module.min.js'), encoding='utf-8').read()
    app = open(os.path.join(HERE, 'app.js'), encoding='utf-8').read()
    html = open(os.path.join(HERE, 'index.html'), encoding='utf-8').read()
    wasm_b64 = base64.b64encode(open(wasm_path, 'rb').read()).decode('ascii')

    bundle = ('const __THREE_CORE__ = %s;\n'
              'globalThis.__THREE = %s;\n'
              % (to_iife(core), to_iife(main_js, '__THREE_CORE__')))

    # у app.js единственный внешний импорт — сам three
    app = app.replace("import * as THREE from 'three';",
                      'const THREE = globalThis.__THREE;')

    # выкидываем import map и внешние подключения, вставляем всё внутрь
    html = re.sub(r'<script type="importmap">.*?</script>', '', html, flags=re.S)
    inline = ('<script>globalThis.__RP_WASM_B64 = "%s";</script>\n'
              '<script>%s</script>\n'
              '<script type="module">\n%s\n</script>' % (wasm_b64, bundle, app))
    html = html.replace('<script type="module" src="./app.js"></script>', inline)

    with open(OUT, 'w', encoding='utf-8') as f:
        f.write(html)
    print('готово: %s  (%.1f МБ)' % (OUT, os.path.getsize(OUT) / 1048576.0))
    return 0


if __name__ == '__main__':
    sys.exit(main())
