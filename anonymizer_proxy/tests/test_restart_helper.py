"""
Тесты хелпера перезапуска прокси.

Проверяют инварианты, без которых «Сохранить и перезапустить» (/api/restart)
и чат-команда «перезапусти прокси» оставляли сервер лежать выключенным
(браузер: «Не удается получить доступ к сайту»): хелпер обязан лежать В ПАКЕТЕ
(каталог data/ в git не хранится и в релизный архив не попадает), команда
запуска — использовать текущий интерпретатор, а сам хелпер — быть автономным
(не импортировать пакет), чтобы перезапуск сработал даже при сломанном .env.

Запуск: python anonymizer_proxy\\tests\\test_restart_helper.py (из корня проекта)
        или python -m anonymizer_proxy.tests.test_restart_helper
"""
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from anonymizer_proxy import main as proxy_main


def test_helper_ships_with_package():
    """Хелпер — отслеживаемый файл пакета, а не артефакт в data/."""
    helper = proxy_main.RESTART_HELPER
    assert helper.is_file(), f"нет файла хелпера: {helper}"
    assert helper.parent.name == "anonymizer_proxy", helper
    assert "data" not in helper.parts, helper   # data/ в релизный архив не идёт
    print("TEST 1 OK: хелпер перезапуска лежит в пакете (попадёт в релиз)")


def test_command_uses_current_interpreter():
    """Команда запуска: текущий интерпретатор + хелпер; spawn её использует."""
    cmd = proxy_main.restart_helper_command()
    assert cmd[0] == sys.executable, cmd
    assert Path(cmd[1]) == proxy_main.RESTART_HELPER, cmd
    assert Path(cmd[1]).is_file(), cmd
    source = inspect.getsource(proxy_main._schedule_proxy_restart)
    assert "restart_helper_command()" in source, source
    # регрессия прежнего бага: хелпер брался из data/ (на чистой установке
    # файла там нет → сервер не поднимался)
    assert '"data" / "restart_helper.py"' not in source, source
    print("TEST 2 OK: команда запуска — sys.executable + хелпер из пакета")


def test_helper_is_standalone():
    """Хелпер запускается ФАЙЛОМ и не импортирует пакет — сработает и при
    сломанном .env/config. Реально не запускаем (он поднимает сервер):
    проверяем синтаксис и отсутствие импорта пакета."""
    src = proxy_main.RESTART_HELPER.read_text(encoding="utf-8")
    compile(src, str(proxy_main.RESTART_HELPER), "exec")   # синтаксис валиден
    assert "import anonymizer_proxy" not in src, "хелпер не должен импортировать пакет"
    assert "from anonymizer_proxy" not in src, "хелпер не должен импортировать пакет"
    assert "PROXY_PORT" in src, "порт хелпер читает из .env сам"
    assert "anonymizer_proxy.main" in src, "хелпер поднимает сервер через .venv"
    print("TEST 3 OK: хелпер автономен (файл, без импорта пакета)")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    test_helper_ships_with_package()
    test_command_uses_current_interpreter()
    test_helper_is_standalone()
    print("\nALL RESTART HELPER TESTS PASSED")


if __name__ == "__main__":
    main()
