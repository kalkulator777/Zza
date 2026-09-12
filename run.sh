#!/bin/sh
# Запуск ZZA ARENA. Можно просто: ./run.sh
cd "$(dirname "$0")" || exit 1
for py in python3.11 python3 python; do
    if command -v "$py" >/dev/null 2>&1; then exec "$py" play.py "$@"; fi
done
echo "Не найден python3. Установите: sudo apt install python3"
exit 1
