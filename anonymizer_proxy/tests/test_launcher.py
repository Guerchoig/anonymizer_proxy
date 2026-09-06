"""
Тесты хелпера ярлыка запуска (launcher): URL страницы настроек, опрос
/health, логика повторов и exit-коды команд check/open. Браузер при тестах
не открывается (используется --no-browser и подмена сетевых вызовов).

Запуск: python anonymizer_proxy\\tests\\test_launcher.py (из корня)
"""
import io
import sys
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from anonymizer_proxy import config, launcher


class _FakeResponse:
    """Минимальный контекст-менеджер, имитирующий ответ urlopen."""

    def __init__(self, status: int):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch_urlopen(fake):
    """Подменить urllib.request.urlopen в модуле launcher (с возвратом)."""
    orig = launcher.urllib.request.urlopen
    launcher.urllib.request.urlopen = fake
    return orig


def _set_proxy(host, port):
    """Подменить хост/порт в конфиге на время теста."""
    old = (config.PROXY["host"], config.PROXY["port"])
    config.PROXY["host"] = host
    config.PROXY["port"] = port
    return old


def _restore_proxy(old):
    config.PROXY["host"], config.PROXY["port"] = old


def test_settings_url_uses_proxy_host_port():
    """URL формы настроек строится из PROXY_HOST/PROXY_PORT"""
    old = _set_proxy("127.0.0.1", 9999)
    try:
        assert launcher.settings_url() == "http://127.0.0.1:9999/env-editor"
    finally:
        _restore_proxy(old)
    print("TEST 1 OK: URL страницы настроек из PROXY_HOST/PROXY_PORT")


def test_settings_url_wildcard_bind():
    """При привязке 0.0.0.0 браузерный адрес — 127.0.0.1"""
    old = _set_proxy("0.0.0.0", 8081)
    try:
        assert launcher.settings_url() == "http://127.0.0.1:8081/env-editor"
    finally:
        _restore_proxy(old)
    print("TEST 2 OK: 0.0.0.0 заменяется на 127.0.0.1")


def test_probe_health_2xx_and_errors():
    """probe_health: True на 2xx, False на 4xx/5xx и сетевых ошибках"""
    old = _set_proxy("127.0.0.1", 8765)

    def fake_ok(url, timeout):
        assert url == "http://127.0.0.1:8765/health"
        assert timeout == launcher.HEALTH_TIMEOUT
        return _FakeResponse(200)

    def fake_404(url, timeout):
        return _FakeResponse(404)

    def fake_down(url, timeout):
        raise ConnectionError("refused")

    try:
        orig = _patch_urlopen(fake_ok)
        try:
            assert launcher.probe_health() is True
        finally:
            launcher.urllib.request.urlopen = orig

        orig = _patch_urlopen(fake_404)
        try:
            assert launcher.probe_health() is False, "404 не должен считаться запуском"
        finally:
            launcher.urllib.request.urlopen = orig

        orig = _patch_urlopen(fake_down)
        try:
            assert launcher.probe_health() is False, "сетевая ошибка — сервер не запущен"
        finally:
            launcher.urllib.request.urlopen = orig
    finally:
        _restore_proxy(old)
    print("TEST 3 OK: probe_health — 2xx запущен, ошибки/4xx — нет")


def test_is_running_retries_until_success():
    """is_running: повторяет опрос (гонка «сервер стартует») и использует
    переданный probe без обращения к сети"""
    calls: List[bool] = [False, False, True]

    def probe():
        return calls.pop(0) if calls else True

    assert launcher.is_running(probe=probe, pause=0) is True
    assert calls == [], "опрос должен остановиться на первом успехе"

    assert launcher.is_running(probe=lambda: False, attempts=2, pause=0) is False
    print("TEST 4 OK: is_running — повторы до успеха / неуспех после лимита")


def test_main_check_exit_codes():
    """check: exit 0 при работающем сервере, 1 — при неработающем"""
    orig = launcher.is_running
    try:
        launcher.is_running = lambda **kw: True
        buf = io.StringIO()
        with redirect_stdout(buf):
            assert launcher.main(["check"]) == 0
        assert "RUNNING" in buf.getvalue()

        launcher.is_running = lambda **kw: False
        buf = io.StringIO()
        with redirect_stdout(buf):
            assert launcher.main(["check"]) == 1
        assert "NOT_RUNNING" in buf.getvalue()
    finally:
        launcher.is_running = orig
    print("TEST 5 OK: check — exit 0/1 по состоянию сервера")


def test_main_open_no_browser():
    """open --no-browser: 0 и печать URL при работающем сервере; 1 — если
    сервер не запущен (браузер не открывается ни в каком случае)"""
    orig = launcher.is_running
    try:
        launcher.is_running = lambda **kw: True
        buf = io.StringIO()
        with redirect_stdout(buf):
            assert launcher.main(["open", "--no-browser"]) == 0
        assert "WOULD_OPEN" in buf.getvalue()
        assert "/env-editor" in buf.getvalue()

        launcher.is_running = lambda **kw: False
        buf = io.StringIO()
        with redirect_stdout(buf):
            assert launcher.main(["open", "--no-browser"]) == 1
        assert "NOT_RUNNING" in buf.getvalue()
    finally:
        launcher.is_running = orig
    print("TEST 6 OK: open --no-browser — URL печатается, браузер не нужен")


def test_settings_url_default_port():
    """Конфиг по умолчанию (без подмены): форма живёт на /env-editor и
    использует порт из PROXY (число)"""
    url = launcher.settings_url()
    assert url.startswith("http://")
    assert url.endswith("/env-editor")
    assert isinstance(config.PROXY["port"], int)
    print("TEST 7 OK: URL по умолчанию — /env-editor на порту из конфига")


if __name__ == "__main__":
    test_settings_url_uses_proxy_host_port()
    test_settings_url_wildcard_bind()
    test_probe_health_2xx_and_errors()
    test_is_running_retries_until_success()
    test_main_check_exit_codes()
    test_main_open_no_browser()
    test_settings_url_default_port()
    print("\nВсе тесты launcher пройдены.")
