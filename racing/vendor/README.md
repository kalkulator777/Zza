# vendor/ — wasmtime, положенный в репозиторий

На игровых машинах есть Python 3.11 и tornado, интернета нет и ставить
ничего нельзя (§1 контракта). Модуль физики `native/testbed/racing_physics.wasm`
грузится через wasmtime, поэтому рантайм лежит здесь, рядом с игрой:
`python3 run.py` находит его сам, системный пакет не нужен.

Что это. Колесо `wasmtime-48.0.0-py3-none-manylinux1_x86_64`, распакованное
как есть: чистый Python на ctypes плюс одна разделяемая библиотека
`wasmtime/linux-x86_64/_libwasmtime.so`. 42 файла, 30,4 МБ на диске.

**Руками здесь не правят ничего.** Дерево раскладывает и проверяет
`tools/vendor_wasmtime.py`:

    python3 tools/vendor_wasmtime.py --check              # цело ли (в test_sim.py)
    python3 tools/vendor_wasmtime.py --wheel <файл.whl>   # разложить заново
    python3 tools/vendor_wasmtime.py --wheel <файл.whl> --pin   # сменить версию

`wasmtime.lock.json` — имя колеса, его SHA-256 и SHA-256 каждого файла.
По нему `--check` ловит и порчу, и ручную правку, и половинчатое обновление,
не имея самого колеса под рукой.

Почему нельзя обойтись без 30 МБ: §8.1 разведки (`docs/PHYSICS_RAPIER_RESEARCH.md`).
Нативная сборка того же крейта расходится с wasm на первом же шаге физики,
то есть сервер и браузер считали бы разное. wasmtime — цена входа.
