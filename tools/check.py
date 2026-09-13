#!/usr/bin/env python3
"""Полная проверка перед отправкой на боевые машины.

    python3 tools/check.py            всё
    python3 tools/check.py --fast     без браузерного прогона

Порядок неслучаен: сначала дешёвое и точное (совпадение физики Python и JS),
потом модульные тесты, потом медленный прогон в настоящем браузере.
"""

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(title, cmd, optional=False):
    print(f"\n{'=' * 68}\n  {title}\n{'=' * 68}")
    r = subprocess.run(cmd, cwd=ROOT)
    if r.returncode == 0:
        return True
    if optional and r.returncode == 2:
        print(f"  (пропущено)")
        return True
    return False


def main():
    fast = "--fast" in sys.argv
    py = sys.executable
    steps = [
        ("Физика: Python против JavaScript", [py, "tools/crosscheck.py"], True),
        ("Трассы", [py, "tools/validate_track.py"], False),
        ("Модульные тесты", [py, "-m", "pytest", "tests/", "-q",
                             "--ignore=tests/browser_smoke.py"], False),
    ]
    if not fast:
        steps.append(("Прогон в браузере", [py, "tests/browser_smoke.py"], False))

    failed = [t for t, cmd, opt in steps if not run(t, cmd, opt)]

    print(f"\n{'=' * 68}")
    if failed:
        print("  НЕ ПРОШЛО: " + ", ".join(failed))
        return 1
    print("  Всё прошло." + ("  (браузерный прогон пропущен)" if fast else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
