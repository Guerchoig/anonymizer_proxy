"""
Хелпер ярлыка запуска: «прокси уже запущен?» и открытие страницы настроек.

Используется скриптами ярлыка (start_proxy.cmd / start_proxy.ps1 на Windows,
start_proxy.command на macOS):

    python -m anonymizer_proxy.launcher check   # exit 0 — сервер отвечает
    python -m anonymizer_proxy.launcher open    # открыть /env-editor в браузере
                                                # (exit 0 — открыто, 1 — не запущен)

Только стандартная библиотека (urllib + webbrowser). Хост/порт читаются из
конфига прокси в момент вызова (.env: PROXY_HOST / PROXY_PORT), поэтому
нестандартный порт учитывается автоматически. `open --no-browser` печатает
URL, не открывая браузер (для тестов и автопроверок).
"""
import argparse
import sys
import time
import urllib.request
import webbrowser
from typing import Callable, Optional

# Конфиг читается в момент вызова (не при импорте), чтобы тесты могли
# подменять значения PROXY.
from anonymizer_proxy import config

HEALTH_TIMEOUT = 2.0   # секунд на один GET /health
CHECK_ATTEMPTS = 3     # попыток опроса при check (гонка «сервер стартует»)
CHECK_PAUSE = 1.0      # пауза между попытками, сек


def _http_host() -> str:
    """Хост для HTTP/браузерного адреса: при привязке «все интерфейсы»
    браузер ходит по цикловому адресу, а не по 0.0.0.0."""
    host = config.PROXY["host"]
    if host in ("0.0.0.0", "::", ""):
        return "127.0.0.1"
    return host


def settings_url() -> str:
    """URL экранной формы настроек на работающем прокси."""
    return f"http://{_http_host()}:{config.PROXY['port']}/env-editor"


def probe_health(host: Optional[str] = None,
                 port: Optional[int] = None) -> bool:
    """Один GET /health; True при любом ответе 2xx."""
    host = _http_host() if host is None else host
    port = config.PROXY["port"] if port is None else port
    try:
        with urllib.request.urlopen(
                f"http://{host}:{port}/health",
                timeout=HEALTH_TIMEOUT) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def is_running(probe: Callable[[], bool] = probe_health,
               attempts: int = CHECK_ATTEMPTS,
               pause: float = CHECK_PAUSE) -> bool:
    """Запущен ли сервер. Несколько попыток с паузой — защита от гонки,
    когда пользователь кликает ярлык во время старта прокси (порт ещё не
    занят): тогда не поднимаем второй экземпляр, а ждём этот."""
    for i in range(attempts):
        if probe():
            return True
        if i < attempts - 1:
            time.sleep(pause)
    return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="anonymizer_proxy.launcher",
        description="Хелпер ярлыка запуска: статус прокси и страница настроек")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="запущен ли сервер (exit 0 — да, 1 — нет)")
    p_open = sub.add_parser(
        "open", help="открыть /env-editor, если сервер запущен (exit 0/1)")
    p_open.add_argument(
        "--no-browser", action="store_true",
        help="не открывать браузер, только напечатать URL (для тестов)")
    args = parser.parse_args(argv)

    running = is_running()
    if args.command == "check":
        print(settings_url())
        print("RUNNING" if running else "NOT_RUNNING")
        return 0 if running else 1

    if not running:
        print("NOT_RUNNING")
        return 1
    url = settings_url()
    if args.no_browser:
        print(f"WOULD_OPEN {url}")
    else:
        webbrowser.open(url)
        print(f"OPENED {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
