"""
Хелпер перезапуска прокси (запускается ОТДЕЛЬНЫМ отвязанным процессом).

Вызывается прокси перед выходом при чат-команде «перезапусти прокси» и при
«Сохранить и перезапустить» в /env-editor:
1. ждёт освобождения порта (старый процесс завершается);
2. запускает сервер через .venv-интерпретатор (в новом окне консоли);
3. опрашивает /health до успеха и пишет результат в data/logs/restart.log.

ВАЖНО (почему файл лежит в пакете, а не в data/): релиз собирается
`git archive` — только отслеживаемые файлы, а каталог data/ в git не хранится
(.gitignore, там PII). Прежний путь data/restart_helper.py существовал только
на машине разработчика: на чистой установке файла нет, прокси после
/api/restart завершался (os._exit) и НЕ поднимался заново — браузер показывал
«Не удается получить доступ к сайту». Теперь файл отслеживается в пакете.

Запускается ФАЙЛОМ (не `-m`): пакет НЕ импортируется, поэтому перезапуск
сработает, даже если .env сломан настолько, что anonymizer_proxy.config не
импортируется. Порт читается из .env напрямую.

Запуск вручную (без перезапускающего процесса): закройте работающий прокси и
выполните `.venv\\Scripts\\python.exe anonymizer_proxy\\restart_helper.py`
(Windows) / `.venv/bin/python anonymizer_proxy/restart_helper.py` из корня проекта.
"""
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent  # корень проекта
LOG = BASE / "data" / "logs" / "restart.log"
HEALTH_TIMEOUT = 90  # секунд на загрузку NER-модели


def log(message: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"[{stamp}] {message}\n")
    except OSError:
        print(message)


def read_port() -> int:
    env = BASE / ".env"
    try:
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("PROXY_PORT="):
                return int(line.split("=", 1)[1].strip())
    except (OSError, ValueError):
        pass
    return 8081


def port_free(port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def health_ok(port: int) -> bool:
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))
            return data.get("status") == "healthy"
    except Exception:
        return False


def main() -> None:
    port = read_port()
    log(f"Рестарт: жду освобождения порта {port}…")
    for _ in range(30):  # до 15 секунд
        if port_free(port):
            break
        time.sleep(0.5)
    else:
        log("Рестарт: порт так и не освободился — сервер не запущен.")

    log("Рестарт: запускаю сервер (.venv)…")
    if sys.platform == "win32":
        # Windows: CREATE_NEW_CONSOLE — сервер поднимается в видимом окне
        # консоли с логами (согласовано с start_proxy.cmd). События
        # Ctrl+C/Ctrl+Break из других консолей (терминал Cline и т.п.)
        # в это окно не доставляются.
        python_exe = BASE / ".venv" / "Scripts" / "python.exe"
        popen_kwargs: dict = {
            "creationflags": subprocess.CREATE_NEW_CONSOLE
            | subprocess.CREATE_NEW_PROCESS_GROUP,
        }
    else:
        # macOS/Linux: нет ни CREATE_NEW_CONSOLE (AttributeError), ни
        # .venv/Scripts — интерпретатор живёт в .venv/bin. start_new_session
        # отвязывает сервер от терминала рестартера (аналог CREATE_NEW_*)
        # и защищает от Ctrl+C из консоли клиента.
        python_exe = BASE / ".venv" / "bin" / "python"
        popen_kwargs = {"start_new_session": True}
    if not python_exe.exists():
        log(f"Рестарт: интерпретатор не найден: {python_exe} — "
            "запустите start_proxy.sh/start_proxy.cmd вручную.")
        return
    subprocess.Popen(
        [str(python_exe), "-m", "anonymizer_proxy.main"],
        cwd=str(BASE),
        **popen_kwargs,
    )

    deadline = time.time() + HEALTH_TIMEOUT
    while time.time() < deadline:
        if health_ok(port):
            log("Рестарт: сервер поднялся, /health отвечает.")
            return
        time.sleep(1)
    log("Рестарт: сервер не поднялся за отведённое время — "
        "проверьте консоль/логи.")


if __name__ == "__main__":
    main()
