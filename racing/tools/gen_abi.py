#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Генератор раскладок общей памяти модуля физики.

Одно описание — ``tools/abi_layout.json`` — разворачивается в три файла:

    native/src/abi_gen.rs       структуры ``#[repr(C)]`` и проверки смещений
    native/abi/abi_layout.py    индексы полей для хозяина на Python
    native/abi/abi_layout.js    то же для браузера

Зачем: раскладка ``CarOut`` нужна в Rust, в Python и в JS. Написанная руками
трижды, она — ровно та же болезнь, ради лечения которой затевался этап:
три описания одной структуры, обязанные совпадать. Здесь описание одно,
а расхождение ловится проверкой, а не в отладчике.

    python3 tools/gen_abi.py            выпустить файлы заново
    python3 tools/gen_abi.py --check    сверить лежащее в репозитории с выводом

``--check`` возвращает 1, если хоть один файл разошёлся, и печатает, какой
именно. Его место — в общем прогоне проверок.

Все поля во всех структурах — f32 или массивы f32, поэтому выравнивание
тривиально: смещение любого поля кратно четырём, размер записи равен
учетверённому числу чисел. Генератор на этом и стоит; если в описании
появится поле другого размера, он остановится с внятной ошибкой,
а не выпустит тихо неверную раскладку.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LAYOUT = os.path.join(HERE, 'abi_layout.json')

OUT_RS = os.path.join(ROOT, 'native', 'src', 'abi_gen.rs')
OUT_PY = os.path.join(ROOT, 'native', 'abi', 'abi_layout.py')
OUT_JS = os.path.join(ROOT, 'native', 'abi', 'abi_layout.js')

BANNER = ('ВЫПУЩЕНО tools/gen_abi.py из tools/abi_layout.json. '
          'Руками не править:\nправка переживёт ровно до первого '
          '`python3 tools/gen_abi.py --check`.')


# --- разбор описания --------------------------------------------------------

def parse_type(text, structs):
    """'f32' | 'f32[4]' | 'WheelOut[4]' -> (базовый тип, длина массива или None)."""
    base, count = text, None
    if text.endswith(']'):
        base, _, tail = text.partition('[')
        count = int(tail[:-1])
    if base != 'f32' and base not in structs:
        raise SystemExit('неизвестный тип поля %r' % text)
    return base, count


def layout(spec):
    """Считает для каждой структуры размер и индексы полей (в f32).

    Возвращает словарь имя -> {'floats', 'size', 'fields': [...]}, где у поля
    появляются ключи ``index`` (номер числа от начала записи), ``offset``
    (байты), ``count`` (длина массива или None) и ``stride`` (шаг элемента
    массива в числах).
    """
    structs = {s['name']: s for s in spec['structs']}
    done = {}
    for s in spec['structs']:
        idx = 0
        fields = []
        for f in s['fields']:
            base, count = parse_type(f['type'], structs)
            if base == 'f32':
                stride = 1
            else:
                if base not in done:
                    raise SystemExit('%s: вложенная %s описана позже, чем нужна'
                                     % (s['name'], base))
                stride = done[base]['floats']
            item = dict(f)
            item['base'] = base
            item['count'] = count
            item['stride'] = stride
            item['index'] = idx
            item['offset'] = idx * 4
            fields.append(item)
            idx += stride * (count if count is not None else 1)
        done[s['name']] = {'name': s['name'], 'doc': s['doc'],
                           'floats': idx, 'size': idx * 4, 'fields': fields}
    return done


def const_name(field):
    return field['name'].upper().lstrip('_') or field['name'].upper()


# --- Rust -------------------------------------------------------------------

def rust_doc(text, indent=''):
    return ''.join('%s/// %s\n' % (indent, line) if line else '%s///\n' % indent
                   for line in text.split('\n'))


def rust_init(st, table):
    """Константа INIT: нули, кроме полей с явным ``init`` в описании."""
    parts = []
    for f in st['fields']:
        if f['base'] == 'f32':
            val = '%s' % float(f.get('init', 0.0))
            body = val if f['count'] is None else '[%s; %d]' % (val, f['count'])
        else:
            one = '%s::INIT' % f['base']
            body = one if f['count'] is None else '[%s; %d]' % (one, f['count'])
        parts.append('        %s: %s,' % (f['name'], body))
    return '\n'.join(parts)


def gen_rust(spec, table):
    o = []
    o.append('// %s\n' % BANNER.replace('\n', '\n// '))
    o.append('//\n')
    o.append('// %s\n' % spec['doc'].replace('\n', '\n// '))
    o.append('\nuse core::mem::{offset_of, size_of};\n')
    o.append('\n/// Версия раскладки. Хозяин обязан сверить, иначе молча '
             'разъедутся раскладки.\n')
    o.append('/// Поднимается при любом изменении размера ЛЮБОЙ структуры, '
             'даже дополнении в хвост:\n')
    o.append('/// совместимость тут по байтам, а не по смыслу полей '
             '(правило в шапке файла).\n')
    o.append('pub const ABI_VERSION: u32 = %d;\n' % spec['abi_version'])
    for c in spec['consts']:
        o.append('\n')
        o.append(rust_doc(c['doc']))
        o.append('pub const %s: %s = %d;\n' % (c['name'], c['rust_type'], c['value']))

    for s in spec['structs']:
        st = table[s['name']]
        o.append('\n')
        o.append(rust_doc(st['doc']))
        o.append('/// %d f32 = %d байт.\n' % (st['floats'], st['size']))
        o.append('#[repr(C)]\n#[derive(Clone, Copy, Default)]\npub struct %s {\n'
                 % st['name'])
        for f in st['fields']:
            for line in f.get('doc', ()):
                o.append('    /// %s\n' % line)
            o.append('    pub %s: %s,\n' % (f['name'], rust_type(f)))
        o.append('}\n')
        o.append('\nimpl %s {\n' % st['name'])
        o.append('    /// Размер записи в байтах.\n')
        o.append('    pub const SIZE: usize = %d;\n' % st['size'])
        o.append('    /// Сколько в записи чисел f32.\n')
        o.append('    pub const FLOATS: usize = %d;\n' % st['floats'])
        o.append('    /// Запись «всё в нуле» — из неё набиваются общие буферы.\n')
        o.append('    pub const INIT: Self = %s {\n%s\n    };\n'
                 % (st['name'], rust_init(st, table)))
        o.append('}\n')
        o.append('\nconst _: () = assert!(size_of::<%s>() == %s::SIZE);\n'
                 % (st['name'], st['name']))
        for f in st['fields']:
            o.append('const _: () = assert!(offset_of!(%s, %s) == %d);\n'
                     % (st['name'], f['name'], f['offset']))

    o.append('\n// --- общие буферы ---------------------------------------'
             '--------------------\n')
    for b in spec['buffers']:
        st = table[b['struct']]
        n = b['count']
        init = b.get('rust_init', '%s::INIT' % st['name'])
        o.append('\n')
        o.append(rust_doc('%s Адрес отдаёт %s().' % (b['doc'], b['export'])))
        if n == 1:
            # Одиночная запись — скаляр, а не массив из одного: так адрес
            # и обращение к полям читаются в Rust без лишнего [0].
            o.append('pub static mut %s: %s = %s;\n' % (b['name'], st['name'], init))
        else:
            o.append('pub static mut %s: [%s; %s] = [%s; %s];\n'
                     % (b['name'], st['name'], n, init, n))
    return ''.join(o)


def rust_type(f):
    if f['count'] is None:
        return f['base']
    return '[%s; %d]' % (f['base'], f['count'])


# --- Python -----------------------------------------------------------------

def gen_py(spec, table):
    o = []
    o.append('# -*- coding: utf-8 -*-\n')
    o.append('"""%s\n\n%s\n"""\n' % (BANNER.replace('\n', ' '), spec['doc']))
    o.append('\nABI_VERSION = %d\n' % spec['abi_version'])
    for c in spec['consts']:
        o.append('\n# %s\n' % c['doc'].replace('\n', '\n# '))
        o.append('%s = %d\n' % (c['name'], c['value']))

    for s in spec['structs']:
        st = table[s['name']]
        o.append('\n\nclass %s(object):\n' % st['name'])
        o.append('    """%s\n\n    %d f32 = %d байт.\n    """\n'
                 % (st['doc'].replace('\n', '\n    '), st['floats'], st['size']))
        o.append('\n    SIZE = %d\n    FLOATS = %d\n' % (st['size'], st['floats']))
        for f in st['fields']:
            name = const_name(f)
            for line in f.get('doc', ()):
                o.append('    # %s\n' % line)
            o.append('    %s = %d\n' % (name, f['index']))
            if f['count'] is not None:
                o.append('    %s_COUNT = %d\n' % (name, f['count']))
                o.append('    %s_STRIDE = %d\n' % (name, f['stride']))

    o.append('\n\n# Общие буферы: имя, структура, сколько записей, экспорт с адресом.\n')
    o.append('BUFFERS = (\n')
    for b in spec['buffers']:
        st = table[b['struct']]
        o.append("    ('%s', %s, %s, '%s'),\n"
                 % (b['name'], st['name'], b['count'], b['export']))
    o.append(')\n')
    return ''.join(o)


# --- JavaScript -------------------------------------------------------------

def gen_js(spec, table):
    o = []
    o.append('// %s\n' % BANNER.replace('\n', '\n// '))
    o.append('//\n')
    o.append('// %s\n' % spec['doc'].replace('\n', '\n// '))
    o.append('\nexport const ABI_VERSION = %d;\n' % spec['abi_version'])
    for c in spec['consts']:
        o.append('\n// %s\n' % c['doc'].replace('\n', '\n// '))
        o.append('export const %s = %d;\n' % (c['name'], c['value']))

    for s in spec['structs']:
        st = table[s['name']]
        o.append('\n// %s\n' % st['doc'].replace('\n', '\n// '))
        o.append('// %d f32 = %d байт.\n' % (st['floats'], st['size']))
        o.append('export const %s = Object.freeze({\n' % st['name'])
        o.append('    SIZE: %d,\n    FLOATS: %d,\n' % (st['size'], st['floats']))
        for f in st['fields']:
            name = const_name(f)
            for line in f.get('doc', ()):
                o.append('    // %s\n' % line)
            o.append('    %s: %d,\n' % (name, f['index']))
            if f['count'] is not None:
                o.append('    %s_COUNT: %d,\n' % (name, f['count']))
                o.append('    %s_STRIDE: %d,\n' % (name, f['stride']))
        o.append('});\n')

    o.append('\n// Общие буферы: имя, структура, сколько записей, экспорт с адресом.\n')
    o.append('export const BUFFERS = Object.freeze([\n')
    for b in spec['buffers']:
        st = table[b['struct']]
        o.append("    ['%s', %s, %s, '%s'],\n"
                 % (b['name'], st['name'], b['count'], b['export']))
    o.append(']);\n')
    return ''.join(o)


# --- запуск -----------------------------------------------------------------

def build():
    spec = json.load(open(LAYOUT, encoding='utf-8'))
    table = layout(spec)
    return [(OUT_RS, gen_rust(spec, table)),
            (OUT_PY, gen_py(spec, table)),
            (OUT_JS, gen_js(spec, table))]


def main(argv):
    check = '--check' in argv
    bad = 0
    for path, text in build():
        rel = os.path.relpath(path, ROOT)
        if check:
            have = None
            if os.path.exists(path):
                have = open(path, encoding='utf-8').read()
            if have != text:
                why = 'нет файла' if have is None else 'расходится с описанием'
                print('РАЗОШЛОСЬ %s: %s' % (rel, why))
                bad += 1
            else:
                print('совпадает %s' % rel)
        else:
            d = os.path.dirname(path)
            if not os.path.isdir(d):
                os.makedirs(d)
            old = open(path, encoding='utf-8').read() if os.path.exists(path) else None
            if old == text:
                print('без изменений %s' % rel)
            else:
                open(path, 'w', encoding='utf-8').write(text)
                print('записано %s (%d Б)' % (rel, len(text.encode('utf-8'))))
    if check and bad:
        print('\n%d файл(ов) не совпадает с tools/abi_layout.json.' % bad)
        print('Почини описание или перевыпусти: python3 tools/gen_abi.py')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
