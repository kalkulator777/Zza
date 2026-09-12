#!/bin/sh
# Запуск «Дурака» двойным кликом или из терминала: ./start.sh
cd "$(dirname "$0")" || exit 1
exec python3 durak.py "$@"
