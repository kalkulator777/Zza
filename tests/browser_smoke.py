#!/usr/bin/env python3
"""Прогон в настоящем браузере: поднимаем сервер, открываем клиентов, играем.

Главная проверка перед отправкой на боевые машины. Два сценария:

  1. Заезд на время в одиночку. Столкновений и бонусов нет, значит серверу
     нечего сообщать клиенту сверх того, что тот уже посчитал сам. Физика
     побитово одинаковая, поэтому поправок предсказания должно быть ~ноль.
     Если их много — разъехались либо код физики, либо тайминг ввода.

  2. Четверо в сети со столкновениями и бонусами. Здесь поправки законны
     (клиент не может знать, что в него сейчас врежутся), проверяется
     работоспособность: лобби, чат, старт, круги, результаты, реванш, F5.

Chromium — не Firefox, поэтому прогон подтверждает логику, но не поведение
конкретного браузера на конкретном железе. Для этого в игре есть F3.
"""

import os
import socket
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from playwright.sync_api import sync_playwright  # noqa: E402

# В контейнере лежит собранный Chromium, версия которого не совпадает с той,
# что ждёт пакет playwright. Указываем путь явно, вместо докачивания.
CHROME = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
LAUNCH = {"args": [
    "--no-sandbox", "--disable-dev-shm-usage",
    "--use-gl=swiftshader", "--enable-unsafe-swiftshader",
    # Без этого Chromium душит requestAnimationFrame во всех окнах, кроме
    # активного, а их здесь четыре. Клиенты переставали успевать за сервером
    # и постоянно пересобирали предсказание — чисто стендовая беда.
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
]}
if os.path.exists(CHROME):
    LAUNCH["executable_path"] = CHROME

TEST_TRACK = "zelenoe_kolco"    # самый короткий круг из боевых


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Server:
    def __init__(self, port):
        self.port = port
        self.proc = None

    def __enter__(self):
        self.proc = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "main.py"),
             "--no-browser", "--port", str(self.port), "--host", "127.0.0.1"],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for _ in range(80):
            try:
                socket.create_connection(("127.0.0.1", self.port), 0.2).close()
                return self
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("сервер не поднялся")

    def __exit__(self, *a):
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


class Client:
    """Один игрок = отдельный контекст браузера.

    Именно контекст, а не вкладка: вкладки одного контекста делят localStorage,
    а там лежит токен игрока — сервер принял бы их за одного человека. На
    боевых машинах у каждого свой браузер, так что это особенность стенда.
    """

    def __init__(self, browser, port, name):
        self.name = name
        self.errors = []
        self.ctx = browser.new_context(viewport={"width": 1280, "height": 720})
        self.page = self.ctx.new_page()
        self.page.on("console", self._console)
        self.page.on("pageerror", lambda e: self.errors.append(f"[{name}] {e}"))
        self.page.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded")
        self.page.wait_for_selector("#screen-menu.active", timeout=15000)
        self.page.fill("#name-input", name)
        self.page.dispatch_event("#name-input", "change")
        self.page.wait_for_timeout(200)

    def _console(self, msg):
        if msg.type == "error":
            self.errors.append(f"[{self.name}] console: {msg.text}")

    def state(self):
        return self.page.evaluate("""() => {
            const a = window.__zza, w = a.world;
            const own = w && w.cars.get(w.you);
            return {
                screen: a.screen,
                players: a.lobby ? a.lobby.players.length : 0,
                tick: w ? w.tick : 0, serverTick: w ? w.serverTick : 0,
                slack: w ? w.slack : 0, lap: own ? own.lap : -1,
                corr: w ? w.stat.corr : 0, snaps: w ? w.stat.snaps : 0,
                maxCorr: w ? w.stat.maxCorrPx : 0, resets: w ? w.stat.resets : 0,
                corrP50: w ? w.corrPercentile(0.5) : 0,
                raceState: w ? w.state : null,
                fps: a.diag ? a.diag.fps : 0,
                frameMs: a.renderer ? a.renderer.frameMs : 0,
                quality: a.renderer ? a.renderer.quality : -1,
                canvas: a.renderer ? [a.renderer.cv.width, a.renderer.cv.height,
                                      a.renderer.cv.clientWidth,
                                      a.renderer.cv.clientHeight] : null,
                longFrames: a.diag ? a.diag.longFrames : -1,
            };
        }""")

    def drive(self, style=0, use_items=True):
        """Автопилот на стороне страницы.

        Реальные клавиши слать нельзя: четыре окна делят фокус клавиатуры,
        и нажатия ушли бы только в активное.
        """
        self.page.evaluate("""({style, useItems}) => {
            const a = window.__zza;
            if (a.__bot) clearInterval(a.__bot);
            a.__bot = setInterval(() => {
                const w = a.world; if (!w || !w.own) return;
                const car = w.own, t = w.track;
                const i = (car.seg + 5 + style) % t.nseg;
                let d = Math.atan2(t.py[i] - car.y, t.px[i] - car.x) - car.a;
                while (d > Math.PI) d -= 2 * Math.PI;
                while (d < -Math.PI) d += 2 * Math.PI;
                let m = 1;
                if (d > 0.06) m |= 8; else if (d < -0.06) m |= 4;
                if (useItems && Math.random() < 0.02) m |= 32;
                a.input.mask = m;
            }, 16);
        }""", {"style": style, "useItems": use_items})

    def stop(self):
        self.page.evaluate(
            "() => { const a=window.__zza; if(a.__bot) clearInterval(a.__bot); a.input.mask=0; }")

    def pick_track(self, tid, laps):
        self.page.select_option("#opt-track", tid)
        self.page.fill("#opt-laps", str(laps))
        self.page.dispatch_event("#opt-laps", "input")


def wait_race_end(client, limit):
    end = time.time() + limit
    while time.time() < end:
        client.page.wait_for_timeout(1500)
        st = client.state()
        if st["screen"] == "results" or st["raceState"] == "done":
            return True
    return False


def scenario_solo(browser, port, fail):
    print("\n=== Сценарий 1: заезд на время в одиночку ===")
    c = Client(browser, port, "Одиночка")
    c.page.click("#btn-solo")
    c.page.wait_for_selector("#dlg-create[open]")
    c.pick_track(TEST_TRACK, 1)
    c.page.click("#dlg-ok")
    c.page.wait_for_selector("#screen-race.active", timeout=8000)
    print("→ заезд начался")
    c.drive(style=0, use_items=False)
    c.page.wait_for_timeout(1500)
    # Размер холста снимаем ВО ВРЕМЯ заезда: после финиша экран уже скрыт,
    # и clientWidth равен нулю.
    cw, ch, cssw, cssh = c.state()["canvas"]
    print(f"→ холст {cw}x{ch} при окне {cssw}x{cssh}")
    if not cssw or not cssh:
        fail("не удалось измерить холст во время заезда")
    elif cw < cssw or ch < cssh:
        fail(f"холст {cw}x{ch} меньше окна {cssw}x{cssh} — рисуем в уменьшенный "
             f"буфер, картинка растянута, а замеры кадра занижены")
    ok = wait_race_end(c, 120)
    c.stop()
    st = c.state()
    print(f"→ круг пройден, тиков {st['tick']}, снапшотов {st['snaps']}, "
          f"fps {st['fps']:.0f}, кадр {st['frameMs']:.2f} мс")
    print(f"→ поправок предсказания: {st['corr']} из {st['snaps']} снапшотов, "
          f"обычная {st['corrP50']:.2f} px, наибольшая {st['maxCorr']:.2f} px")

    if not ok:
        fail("одиночный заезд не завершился за 120 с")
    # Без столкновений и бонусов серверу нечего сообщать клиенту сверх того,
    # что тот уже посчитал сам. Редкие поправки допустимы — это возврат на
    # трассу, законный телепорт. Массовые означали бы расхождение физики.
    share = st["corr"] / max(1, st["snaps"])
    if share > 0.03:
        fail(f"без столкновений предсказание поправлялось в {share*100:.1f}% снапшотов — "
             f"физика клиента и сервера разъехалась")
    if st["resets"] > 1:
        fail(f"предсказание пересобиралось {st['resets']} раз")

    c.page.wait_for_selector("#screen-results.active", timeout=15000)
    recs = c.page.inner_text("#res-records")
    print(f"→ таблица рекордов:\n   {recs.strip()[:120]}")
    if "Одиночка" not in recs:
        fail("рекорд заезда на время не попал в таблицу")
    return c


def scenario_multi(browser, port, fail, n=4):
    print(f"\n=== Сценарий 2: {n} игрока в сети ===")
    clients = [Client(browser, port, f"Игрок{i+1}") for i in range(n)]
    host = clients[0]

    host.page.click("#btn-create")
    host.page.wait_for_selector("#dlg-create[open]")
    host.pick_track(TEST_TRACK, 1)
    host.page.click("#dlg-ok")
    host.page.wait_for_selector("#screen-lobby.active", timeout=8000)
    code = host.page.inner_text("#lobby-code")
    print(f"→ лобби {code}")

    for c in clients[1:]:
        c.page.fill("#join-code", code)
        c.page.click("#btn-join-code")
        c.page.wait_for_selector("#screen-lobby.active", timeout=8000)
    host.page.wait_for_timeout(400)
    if host.state()["players"] != n:
        fail(f"в лобби {host.state()['players']} игроков вместо {n}")
    print(f"→ собрались все {n}")

    clients[1].page.fill("#chat-input", "поехали")
    clients[1].page.press("#chat-input", "Enter")
    host.page.wait_for_timeout(300)
    if "поехали" not in host.page.inner_text("#chat-log"):
        fail("сообщение чата не дошло")

    # Диагностика: без неё на боевых машинах нечем разбираться
    host.page.keyboard.press("F3")
    host.page.wait_for_timeout(300)
    if host.page.is_hidden("#diag"):
        fail("оверлей диагностики (F3) не открылся")
    elif "fps" not in host.page.inner_text("#diag"):
        fail("оверлей диагностики пуст")
    else:
        print("→ F3 показывает телеметрию")
    host.page.keyboard.press("F3")

    # Живая настройка физики: правка хозяина должна долететь до всех
    host.page.keyboard.press("F4")
    host.page.wait_for_timeout(300)
    if host.page.is_hidden("#tuner"):
        fail("панель настройки физики (F4) не открылась")
    else:
        host.page.evaluate("""() => {
            const a = window.__zza;
            a.net.send({t:'tune', section:'car', key:'maxSpeed', value: 321});
        }""")
        host.page.wait_for_timeout(500)
        got = clients[1].page.evaluate("() => window.__zza.phys.car.maxSpeed")
        if got != 321:
            fail(f"правка физики не дошла до другого игрока (у него {got})")
        else:
            print("→ F4: правка физики разошлась по всем")
        host.page.evaluate("""() => window.__zza.net.send(
            {t:'tune', section:'car', key:'maxSpeed', value: 430})""")
        host.page.wait_for_timeout(300)
    host.page.keyboard.press("F4")

    # Не-хозяин физику крутить не должен
    clients[1].page.evaluate("""() => window.__zza.net.send(
        {t:'tune', section:'car', key:'maxSpeed', value: 999})""")
    host.page.wait_for_timeout(400)
    if host.page.evaluate("() => window.__zza.phys.car.maxSpeed") == 999:
        fail("физику смог поменять не хозяин лобби")
    else:
        print("→ не-хозяину настройка физики запрещена")

    host.page.click("#btn-start")
    for c in clients:
        c.page.wait_for_selector("#screen-race.active", timeout=8000)
    print("→ заезд начался у всех")

    for i, c in enumerate(clients):
        c.drive(style=i * 2)
    ok = wait_race_end(host, 180)
    for c in clients:
        c.stop()

    states = [c.state() for c in clients]
    print("\n  клиент     тик  серв   запас  поправ/снапш  макс.px  перес  fps  кадр мс  кач  >20мс")
    for c, st in zip(clients, states):
        print(f"  {c.name:9} {st['tick']:6} {st['serverTick']:6} {st['slack']:6} "
              f"{st['corr']:6}/{st['snaps']:<6} {st['maxCorr']:8.1f} {st['resets']:5} "
              f"{st['fps']:5.0f} {st['frameMs']:7.2f} {st['quality']:4} {st['longFrames']:6}")

    if not ok:
        fail("сетевой заезд не завершился за 180 с")
    for c, st in zip(clients, states):
        if st["snaps"] < 20:
            fail(f"{c.name}: снапшотов всего {st['snaps']}")
        if st["resets"] > 2:
            fail(f"{c.name}: предсказание пересобиралось {st['resets']} раз")
        if st["slack"] < -3:
            fail(f"{c.name}: ввод опаздывает, запас {st['slack']}")
        # Порог низкий намеренно: четыре Chromium с программным GL делят одно
        # ядро контейнера. Показательно здесь время нашего рендера, а не fps.
        if st["fps"] < 15:
            fail(f"{c.name}: fps всего {st['fps']:.0f}")
        if st["frameMs"] > 8:
            fail(f"{c.name}: рендер занимает {st['frameMs']:.1f} мс на кадр")
        if st["lap"] < 1:
            fail(f"{c.name}: не проехал ни круга")

    host.page.wait_for_selector("#screen-results.active", timeout=25000)
    table = host.page.inner_text("#res-table")
    print(f"\n→ результаты:\n{table}")
    for c in clients:
        if c.name not in table:
            fail(f"{c.name} отсутствует в таблице результатов")

    host.page.click("#btn-rematch")
    host.page.wait_for_selector("#screen-lobby.active", timeout=8000)
    print("→ реванш вернул в лобби")

    clients[1].page.reload(wait_until="domcontentloaded")
    clients[1].page.wait_for_timeout(2000)
    if clients[1].state()["screen"] != "lobby":
        fail(f"после F5 игрок не вернулся в лобби "
             f"(экран {clients[1].state()['screen']})")
    else:
        print("→ после F5 игрок вернулся в своё лобби")

    return clients


def main():
    port = free_port()
    failures = []

    def fail(msg):
        failures.append(msg)

    with Server(port), sync_playwright() as pw:
        browser = pw.chromium.launch(**LAUNCH)
        everyone = []
        try:
            everyone.append(scenario_solo(browser, port, fail))
            everyone.extend(scenario_multi(browser, port, fail))
        finally:
            errors = [e for c in everyone for e in c.errors]
            browser.close()

    if errors:
        print("\nОШИБКИ В КОНСОЛИ БРАУЗЕРА:")
        for e in errors[:25]:
            print("  " + e)
        failures.append(f"ошибок в консоли браузера: {len(errors)}")

    print()
    if failures:
        print("ПРОВАЛЫ:")
        for f in failures:
            print("  ! " + f)
        return 1
    print("Браузерный прогон пройден полностью.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
