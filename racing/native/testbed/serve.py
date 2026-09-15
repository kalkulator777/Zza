#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Крошечная статика для площадки катания.

Отдаёт этот каталог, а путь /vendor/ подкладывает из static/vendor игры,
чтобы не плодить вторую копию three.js. Игру не трогает и не запускает.

    python3 native/testbed/serve.py [порт]
    ->  http://127.0.0.1:8777/
"""
import http.server
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.normpath(os.path.join(HERE, '..', '..', 'static', 'vendor'))
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8777


class Handler(http.server.SimpleHTTPRequestHandler):
    extensions_map = dict(http.server.SimpleHTTPRequestHandler.extensions_map)
    extensions_map['.wasm'] = 'application/wasm'
    extensions_map['.js'] = 'text/javascript'

    def translate_path(self, path):
        clean = path.split('?', 1)[0].split('#', 1)[0]
        if clean.startswith('/vendor/'):
            name = os.path.basename(clean)
            return os.path.join(VENDOR, name)
        return http.server.SimpleHTTPRequestHandler.translate_path(self, path)

    def log_message(self, fmt, *args):
        pass


def main():
    os.chdir(HERE)
    if not os.path.exists(os.path.join(HERE, 'racing_physics.wasm')):
        print('нет racing_physics.wasm рядом со страницей — собери крейт:')
        print('  cd native && cargo build --release --target wasm32-unknown-unknown')
        return 1
    srv = http.server.ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    print('площадка: http://127.0.0.1:%d/   (Ctrl+C чтобы остановить)' % PORT)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
