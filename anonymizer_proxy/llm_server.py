"""
Менеджер llama-server (llama.cpp) — локальный LLM-бэкенд проекта.

llama-server — общий сервис машины: одна GGUF-модель в памяти, OpenAI-
совместимый HTTP API (порт 8080 по умолчанию). К нему ходят и прокси
(anonymizer_proxy), и другие приложения (RAG, MCP-серверы). Этот модуль:

- ПРОВЕРЯЕТ при старте прокси, нет ли уже живого инстанса llama-server
  на LLM_SERVER_HOST:PORT и в каком режиме он запущен. Живой инстанс
  ПЕРЕИСПОЛЬЗУЕТСЯ, второй не запускается (аналог launcher.is_running()
  для самого прокси);
- ЗАПУСКАЕТ сервер с явными флагами (--host --port --parallel
  --ctx-size), отдельным процессом (переживает перезапуск прокси) и
  логом в data/logs/llama_server.log;
- останавливает (по PID-файлу) и отдаёт статус (слоты, pid, модель).

CLI:
    python -m anonymizer_proxy.llm_server check     # exit 0 — живой llama
    python -m anonymizer_proxy.llm_server start     # проверить и запустить
    python -m anonymizer_proxy.llm_server stop      # остановить по PID
    python -m anonymizer_proxy.llm_server status    # JSON-статус
    python -m anonymizer_proxy.llm_server restart   # stop + start

Только стандартная библиотека (urllib/subprocess). Конфигурация — секция
LLM_SERVER в config (.env: LLM_SERVER_*).
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Optional

from .config import BASE_DIR, DATA_DIR, LLM_SERVER, logger
from . import llama_runtime

PID_FILE = DATA_DIR / "llama_server.pid"
LOG_FILE = DATA_DIR / "logs" / "llama_server.log"

# Состояния порта (результат probe())
STATE_LLAMA = "llama"        # живой llama-server (есть /props с total_slots)
STATE_FOREIGN = "foreign"    # порт занят посторонним HTTP-сервисом
STATE_DOWN = "down"          # никто не слушает

_PROBE_TIMEOUT = 3.0         # сек на один GET /health или /props
_POLL_INTERVAL = 2.0         # сек между опросами /health при старте


# ==================== HTTP-пробы ====================

def _get_json(url: str, timeout: float = _PROBE_TIMEOUT) -> dict:
    """GET с таймаутом; dict при 2xx-JSON, иначе исключение."""
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if not 200 <= resp.status < 300:
            raise OSError(f"HTTP {resp.status}")
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def base_url(cfg: Optional[dict] = None) -> str:
    """Адрес llama-server без /v1."""
    cfg = cfg or LLM_SERVER
    return f"http://{cfg['host']}:{cfg['port']}"


def probe(cfg: Optional[dict] = None, timeout: float = _PROBE_TIMEOUT) -> dict:
    """Определить, кто слушает LLM_SERVER_HOST:PORT.

    Живой llama распознаётся по GET /props с целым total_slots >= 1 —
    посторонний сервис на этом порту llama-формата не вернёт.
    Возвращает {"state": STATE_*, "total_slots": int|None, "props": dict}.
    """
    cfg = cfg or LLM_SERVER
    url = base_url(cfg)
    try:
        with urllib.request.urlopen(f"{url}/health", timeout=timeout) as resp:
            if not 200 <= resp.status < 300:
                raise OSError(f"HTTP {resp.status}")
    except Exception:
        return {"state": STATE_DOWN, "total_slots": None, "props": {}}

    try:
        props = _get_json(f"{url}/props", timeout)
        slots = props.get("total_slots")
        if isinstance(slots, int) and slots >= 1:
            return {"state": STATE_LLAMA, "total_slots": slots, "props": props}
        return {"state": STATE_FOREIGN, "total_slots": None, "props": {}}
    except Exception:
        return {"state": STATE_FOREIGN, "total_slots": None, "props": {}}


# ==================== Автопоиск бинаря ====================

def find_binary(cfg: Optional[dict] = None) -> str:
    """Путь к llama-server: LLM_SERVER_BIN > общий llama-рантайм машины >
    tools/llama.cpp/ проекта > PATH. Пустая строка — не найден.

    Общий рантайм — единое место бинаря для всех проектов машины
    (см. llama_runtime.py: %LLAMA_RUNTIME_DIR% или
    %LOCALAPPDATA%\\llama-runtime).
    """
    cfg = cfg or LLM_SERVER
    if cfg["bin"]:
        return cfg["bin"]
    shared = llama_runtime.find_binary()
    if shared:
        return shared
    exe = "llama-server.exe" if sys.platform == "win32" else "llama-server"
    # корень проекта, а не cwd: запуск из другого каталога не должен
    # «терять» установленный в проекте tools/llama.cpp
    local = BASE_DIR / "tools" / "llama.cpp" / exe
    if local.is_file():
        return str(local)
    found = shutil.which("llama-server")
    return str(found) if found else ""

# ==================== Команда запуска ====================

def build_command(cfg: Optional[dict] = None) -> list:
    """Командная строка llama-server из секции LLM_SERVER.

    Контекст задаётся общим буфером --ctx-size = parallel × ctx_per_slot:
    при PARALLEL=1 единственный слот получает ровно ctx_per_slot (32K по
    умолчанию), при PARALLEL>1 каждый слот получает свою долю того же
    размера. --port передаётся ВСЕГДА явно (не полагаемся на дефолт
    бинаря). WebUI llama-server отключён: единственный UI проекта —
    страница /env-editor.
    """
    cfg = cfg or LLM_SERVER
    bin_path = find_binary(cfg)
    if not bin_path:
        raise RuntimeError(
            "LLM_SERVER_BIN не задан, а llama-server не найден ни в общем "
            "llama-рантайме (%LOCALAPPDATA%\\llama-runtime\\bin), ни в "
            "PATH, ни в tools/llama.cpp/. Установите llama.cpp или укажите "
            "путь к бинарю в .env (LLM_SERVER_BIN).")
    if not cfg["model"]:
        raise RuntimeError(
            "LLM_SERVER_MODEL не задан — укажите shared:chat (общая "
            "чат-модель llama-рантайма) или путь к GGUF-файлу в .env")
    # shared:chat — файл из манифеста общего рантайма (models/chat/
    # current.json): одна модель на все проекты, смена — одной командой
    try:
        model_path = llama_runtime.resolve_model(cfg["model"], role="chat")
    except FileNotFoundError as exc:
        raise RuntimeError(str(exc)) from exc
    if not model_path.is_absolute():
        model_path = BASE_DIR / model_path
    if not model_path.is_file():
        raise RuntimeError(
            f"GGUF-модель не найдена: {model_path}. Укажите корректный "
            "путь в .env (LLM_SERVER_MODEL) или shared:chat")

    parallel = max(1, int(cfg["parallel"]))
    ctx_per_slot = int(cfg["ctx_per_slot"])
    total_ctx = parallel * ctx_per_slot
    if ctx_per_slot < 32768:
        logger.warning(
            "LLM_SERVER_CTX_PER_SLOT=%s — меньше проектных 32K: длинные "
            "файлы и RAG-поиски упадут с ошибкой 'request exceeds the "
            "available context size'", ctx_per_slot)

    cmd = [
        bin_path,
        "-m", str(model_path),
        "--host", str(cfg["host"]),
        "--port", str(cfg["port"]),
        "--parallel", str(parallel),
        "--ctx-size", str(total_ctx),
        # WebUI llama-server не нужен: единственный UI проекта — /env-editor
        "--no-webui",
        "--cache-reuse", "256",
    ]
    if cfg["api_key"]:
        cmd += ["--api-key", str(cfg["api_key"])]
    extra = (cfg["extra_args"] or "").split()
    if extra:
        # если пользователь включил WebUI в extra_args — не глушим его
        if "--no-webui" in cmd and any(
                a in ("--webui", "--ui") for a in extra):
            cmd.remove("--no-webui")
        cmd += extra
    return cmd


# ==================== Запуск / остановка ====================

def _popen_kwargs() -> dict:
    """Отвязанный запуск: llama-server переживает перезапуск прокси."""
    if sys.platform == "win32":
        return {"creationflags": (subprocess.DETACHED_PROCESS
                                  | subprocess.CREATE_NEW_PROCESS_GROUP)}
    return {"start_new_session": True}


def _log_path() -> Path:
    """Путь к логу llama-server (каталог создаётся при необходимости)."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    return LOG_FILE


def _read_pid() -> Optional[int]:
    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


def start(cfg: Optional[dict] = None, wait: bool = True) -> dict:
    """Проверить порт и при необходимости запустить llama-server.

    Живой llama-инстанс НЕ трогаем (reuse) — в том числе запущенный
    пользователем вручную или другим приложением. Посторонний сервис на
    порту — ошибка с подсказкой (молча менять порт нельзя: на этот адрес
    смотрят клиенты).

    Возвращает {"started": bool, "reused": bool, "state": str,
    "total_slots": int|None, "pid": int|None, "command": list}.
    """
    cfg = cfg or LLM_SERVER
    det = probe(cfg)
    if det["state"] == STATE_LLAMA:
        slots = det["total_slots"]
        need = max(1, int(cfg["parallel"]))
        if slots < need:
            logger.warning(
                "llama-server уже запущен на %s с %s слот(ами) — меньше "
                "требуемых %s (LLM_SERVER_PARALLEL). Переиспользую чужой "
                "инстанс без перезапуска; запросы разделят его контекст. "
                "Для заданного конкурентного режима выполните: "
                "python -m anonymizer_proxy.llm_server restart",
                base_url(cfg), slots, need)
        logger.info("llama-server уже запущен на %s (%s слот(ов)) — "
                    "переиспользую", base_url(cfg), slots)
        return {"started": False, "reused": True, "state": det["state"],
                "total_slots": slots, "pid": _read_pid(), "command": []}
    if det["state"] == STATE_FOREIGN:
        raise RuntimeError(
            f"Порт {cfg['host']}:{cfg['port']} занят посторонним сервисом "
            f"(не llama-server). Освободите порт или смените LLM_SERVER_PORT "
            f"в .env (и LOCAL_LLM_BASE_URL, если он задан явно).")

    cmd = build_command(cfg)
    log_path = _log_path()
    with open(log_path, "ab") as log:
        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=log,
                stderr=subprocess.STDOUT, **_popen_kwargs())
        except OSError as exc:
            raise RuntimeError(
                f"Не удалось запустить llama-server ({cmd[0]}): {exc}. "
                "Проверьте LLM_SERVER_BIN в .env.") from exc
    PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    logger.info("llama-server запущен (pid %s): %s", proc.pid, " ".join(cmd))
    logger.info("Лог llama-server: %s", log_path)

    if not wait:
        return {"started": True, "reused": False, "state": STATE_DOWN,
                "total_slots": None, "pid": proc.pid, "command": cmd}

    # Ожидание готовности: загрузка GGUF и выделение KV-кэша занимают
    # десятки секунд — поллим /health до таймаута
    deadline = time.monotonic() + float(cfg["start_timeout"])
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"llama-server завершился с кодом {proc.returncode} при "
                f"старте — см. лог {log_path}")
        det = probe(cfg)
        if det["state"] == STATE_LLAMA:
            logger.info("llama-server готов: %s (%s слот(ов))",
                        base_url(cfg), det["total_slots"])
            return {"started": True, "reused": False, "state": STATE_LLAMA,
                    "total_slots": det["total_slots"], "pid": proc.pid,
                    "command": cmd}
        time.sleep(_POLL_INTERVAL)
    raise RuntimeError(
        f"llama-server не ответил на /health за {cfg['start_timeout']} c — "
        f"см. лог {log_path}")


def stop(cfg: Optional[dict] = None) -> bool:
    """Остановить llama-server по PID-файлу. True — процесс был и убит.

    Чужой llama-инстанс, запущенный вне менеджера (нет PID-файла), не
    трогаем — возвращаем False с предупреждением.
    """
    if not PID_FILE.is_file():
        logger.warning(
            "PID-файл %s отсутствует — llama-server, вероятно, запущен "
            "вне менеджера; останавливать нечего", PID_FILE)
        return False
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    except (ValueError, OSError) as exc:
        logger.warning("Битый PID-файл %s: %s", PID_FILE, exc)
        PID_FILE.unlink(missing_ok=True)
        return False
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, check=False)
        else:
            os.kill(pid, 15)  # SIGTERM
    except (ProcessLookupError, OSError) as exc:
        logger.info("Процесс %s уже не работает (%s)", pid, exc)
    PID_FILE.unlink(missing_ok=True)
    logger.info("llama-server (pid %s) остановлен", pid)
    return True


# ==================== Статус ====================

def _live_pid() -> Optional[int]:
    """PID из PID-файла, если процесс с ним ещё жив."""
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None
    if sys.platform == "win32":
        res = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                             capture_output=True, text=True, check=False)
        return pid if str(pid) in (res.stdout or "") else None
    try:
        os.kill(pid, 0)
        return pid
    except OSError:
        return None


def status(cfg: Optional[dict] = None) -> dict:
    """Сводный статус llama-server (для /api/backend и self-test)."""
    cfg = cfg or LLM_SERVER
    det = probe(cfg)
    return {
        "base_url": base_url(cfg),
        "state": det["state"],
        "running": det["state"] == STATE_LLAMA,
        "total_slots": det["total_slots"],
        "configured_parallel": cfg["parallel"],
        "ctx_per_slot": cfg["ctx_per_slot"],
        "model": cfg["model"],
        "pid": _live_pid(),
        "model_info": (det["props"].get("model_path")
                       or det["props"].get("model") or ""),
    }


def ensure(cfg: Optional[dict] = None) -> dict:
    """Гарантировать живой llama-server: переиспользовать или запустить.

    Вызывается при старте прокси (неблокирующе, в фоне) и командой
    `llm_server start`. Ошибки НЕ роняют прокси — вызывающий логирует
    исключение: облачные бэкенды работают и без llama-server.
    """
    return start(cfg, wait=True)


# ==================== CLI ====================

def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(
        prog="anonymizer_proxy.llm_server",
        description="Менеджер llama-server (llama.cpp) — локального "
                    "LLM-бэкенда прокси")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="живой llama-server на порту? (exit 0/1)")
    sub.add_parser("start", help="проверить порт и запустить сервер")
    sub.add_parser("stop", help="остановить сервер по PID-файлу")
    sub.add_parser("status", help="JSON-статус (слоты, pid, модель)")
    sub.add_parser("restart", help="stop + start")
    sub.add_parser("run", help="запустить llama-server в foreground "
                               "(для LaunchAgent/KeepAlive)")
    args = parser.parse_args(argv)

    if args.command == "run":
        cmd = build_command()
        logger.info("llama-server (foreground): %s", " ".join(cmd))
        if sys.platform == "win32":
            return subprocess.call(cmd)
        os.execvp(cmd[0], cmd)  # заменяем процесс: сигналы launchd родные

    if args.command == "check":
        det = probe()
        print(f"{base_url()}: state={det['state']} "
              f"total_slots={det['total_slots']}")
        return 0 if det["state"] == STATE_LLAMA else 1
    if args.command == "start":
        try:
            info = start()
        except RuntimeError as exc:
            print(f"[ОШИБКА] {exc}", file=sys.stderr)
            return 1
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0
    if args.command == "stop":
        return 0 if stop() else 1
    if args.command == "status":
        print(json.dumps(status(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "restart":
        stop()
        try:
            info = start()
        except RuntimeError as exc:
            print(f"[ОШИБКА] {exc}", file=sys.stderr)
            return 1
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
