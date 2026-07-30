"""Дневник как приложение: сервер и синхронизация в одном процессе.

До этого дневник поднимался руками (`serve`), а данные обновлял отдельный
LaunchAgent раз в полчаса. Обе половины работали, но вместе давали дневник,
который открывают раз в неделю и который после сна мака показывает вчерашние
цифры: `StartInterval` + `ProcessType = Background` система откладывает, и
пропущенный за сон запуск не догонялся (28.07 разрыв составил 3 ч 45 мин).

Здесь этого нет по конструкции: процесс живёт всегда (LaunchAgent с
`KeepAlive`), а расписание — обычный `sleep` в потоке. Заснул мак — поток
проснулся вместе с ним и пошёл на круг.

Синхронизация не продублирована на Python: поток запускает тот же
`deploy/sync.sh`, что и раньше запускался по расписанию, и что запускается
руками. Одна дорога вместо двух — иначе ручной и автоматический синк
разъезжаются, и чинить приходится дважды.
"""

import subprocess
import threading
import time
from pathlib import Path

from . import db, server

PROJECT_DIR = Path(__file__).resolve().parent.parent
SYNC_SCRIPT = PROJECT_DIR / "deploy" / "sync.sh"

# Раз в минуту: биржа отдаёт fills сразу, а два запроса в минуту далеко от
# лимитов Bybit. Инкрементальный забор берёт только новое, поэтому круг стоит
# столько же, сколько один HTTP-запрос.
INTERVAL_SEC = 60
# Круг не может идти дольше собственного интервала бесконечно: если ssh завис,
# лучше оборвать и попробовать в следующий раз, чем накопить очередь потоков.
TIMEOUT_SEC = 300
# sync.sh вышел так, когда сети нет и круг пропущен. Не успех и не поломка:
# штамп свежести не двигается (данные и правда старее), но и жаловаться не на
# что — интерфейс сам покажет растущее «обновлено N мин назад».
SKIPPED_RC = 75


class SyncLoop:
    """Фоновая синхронизация. Знает, чем закончился последний круг.

    Состояние нужно интерфейсу: дневник со сломанным обновлением выглядит
    исправным, поэтому «когда в последний раз получилось» — часть данных,
    а не деталь реализации.
    """

    def __init__(self, script: Path = SYNC_SCRIPT, interval: int = INTERVAL_SEC):
        self.script = script
        self.interval = interval
        self.state = {"last_ok": None, "last_error": None, "running": False}
        self._stop = threading.Event()

    def run_once(self) -> bool:
        self.state["running"] = True
        try:
            result = subprocess.run(
                ["/bin/bash", str(self.script)],
                cwd=str(PROJECT_DIR), capture_output=True, text=True,
                timeout=TIMEOUT_SEC,
            )
            if result.returncode == 0:
                self.state["last_ok"] = int(time.time() * 1000)
                self.state["last_error"] = None
                return True

            # Пропуск по отсутствию сети: ничего не синхронизировано, но и
            # чинить нечего. Оба штампа остаются как были.
            if result.returncode == SKIPPED_RC:
                return False

            # Провал — единственное, что попадает в лог: круг проходит раз в
            # минуту, и удачные оставили бы четырнадцать тысяч строк в сутки,
            # в которых настоящая поломка утонула бы. Что синк работает, видно
            # в интерфейсе; лог отвечает на вопрос «почему сломался», поэтому
            # здесь нужен весь вывод, а не одна строка.
            output = (result.stdout or "") + (result.stderr or "")
            print(f"--- синхронизация не прошла (код {result.returncode}) ---\n"
                  f"{output.strip()}", flush=True)
            tail = output.strip().splitlines()
            self.state["last_error"] = tail[-1] if tail else f"код {result.returncode}"
        except subprocess.TimeoutExpired:
            self.state["last_error"] = f"синк не уложился в {TIMEOUT_SEC} с"
        except OSError as exc:
            self.state["last_error"] = str(exc)
        finally:
            self.state["running"] = False
        return False

    def loop(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            self._stop.wait(self.interval)

    def start(self) -> None:
        threading.Thread(target=self.loop, daemon=True, name="sync").start()

    def stop(self) -> None:
        self._stop.set()


def run(port: int = 8321, host: str = "127.0.0.1", *, sync: bool = True) -> None:
    # Ошибка настройки ловится до того, как что-то поднято и что-то изменено.
    if sync and not SYNC_SCRIPT.exists():
        raise FileNotFoundError(f"нет скрипта синхронизации: {SYNC_SCRIPT}")

    conn = db.connect()
    conn.close()          # схема и миграции — до того, как поднимется сервер

    loop = SyncLoop()
    if sync:
        server.Handler.sync_state = loop.state
        loop.start()
        print(f"Синхронизация: раз в {INTERVAL_SEC} с", flush=True)

    try:
        server.serve(port=port, host=host)
    finally:
        loop.stop()
