"""
Общий llama-рантайм машины — единое место llama-server и GGUF-моделей
для всех проектов (anonymizer_proxy, hermes-disk-search и др.).

SYNC-COPY: файл идентичен в обоих репозиториях
(anonymizer_proxy/llama_runtime.py и hds/llama_runtime.py).
При правке синхронизировать копии вручную (пометка SYNC-COPY).

Каталог рантайма: %LLAMA_RUNTIME_DIR%, иначе по ОС:
  Windows — %LOCALAPPDATA%\\llama-runtime
  macOS   — ~/Library/Application Support/llama-runtime
  прочие  — ~/.local/share/llama-runtime (XDG)

Раскладка:
  bin\\                       — llama-server(.exe) + все DLL (ОДНА сборка
                                cuda|vulkan на машину)
  models\\<role>\\            — GGUF-файлы роли (chat, embedding, rerank…)
  models\\chat\\current.json  — манифест активной чат-модели:
                                {"file": "Qwen3.5-9B-Q6_K.gguf",
                                 "switched_at": "2026-09-24T…"}
  projects.json              — реестр проектов-потребителей (для
                                перезапуска их llama-инстансов при смене
                                чат-модели): [{"name", "root",
                                "restart_args": ["-m", "…", "restart", …]}]
  version.json               — {"variant": "cuda|vulkan", "tag": …,
                                "installed_at": …} — защита от смешивания
                                разных вариантов сборки

Модель-спецификатор "shared:<role>" в конфигах проектов означает
«файл из манифеста общего рантайма» (models/<role>/current.json) — так
смена модели одной командой видна всем проектам сразу.

API (используют менеджеры llama-server проектов, их UI и ensure-скрипт):
  runtime_dir()/bin_dir()/models_dir(role)  — пути
  find_binary()                             — llama-server из bin\\
  resolve_model(spec, role)                 — shared:<role> → путь GGUF
  chat_models_overview()                    — данные для UI (список,
                                              пресеты, текущая, статус)
  download_chat_model(name, progress)       — скачивание пресета
  switch_chat_model(file, …)                — смена модели + перезапуск
                                              llama-инстансов проектов
  register_project(name, root, restart_args)

Только стандартная библиотека (как llm_server.py).
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Callable, Optional

RUNTIME_DIR_ENV = "LLAMA_RUNTIME_DIR"
DEFAULT_DIRNAME = "llama-runtime"
SHARED_PREFIX = "shared:"

# ==================== Пресеты чат-моделей ====================
# Имя файла в models/chat/ → источник. Пополнение = одна запись в словарь
# (файл появится в списке UI как пресет «скачать»).
CHAT_PRESETS = {
    "Qwen3.5-9B-Q6_K.gguf": {
        "url": "https://huggingface.co/unsloth/Qwen3.5-9B-GGUF/resolve/main/Qwen3.5-9B-Q6_K.gguf",
        "min_bytes": 4_000_000_000,
        "size_hint": "~7.5 ГБ",
        "desc": "Qwen3.5-9B Q6_K (высокое качество)",
    },
    "Qwen3.5-9B-Instruct-Q4_K_M.gguf": {
        "url": "https://huggingface.co/unsloth/Qwen3.5-9B-Instruct-GGUF/resolve/main/Qwen3.5-9B-Instruct-Q4_K_M.gguf",
        "min_bytes": 3_000_000_000,
        "size_hint": "~5.5 ГБ",
        "desc": "Qwen3.5-9B-Instruct Q4_K_M (экономия памяти)",
    },
}
# Чат-модель по умолчанию для новой установки (ensure-скрипт/миграция)
DEFAULT_CHAT = "Qwen3.5-9B-Q6_K.gguf"

# GGUF не-чатовых ролей (общий рантайм хранит и их; скачивает
# ensure_llama_runtime.ps1, но CLI-функция пригодится и здесь)
ROLE_PRESETS = {
    "embedding": {
        "bge-m3-Q8_0.gguf": {
            "url": "https://huggingface.co/lm-kit/bge-m3-gguf/resolve/main/bge-m3-Q8_0.gguf",
            "min_bytes": 300_000_000,
            "size_hint": "~0.6 ГБ",
            "desc": "BAAI bge-m3 (эмбеддинги)",
        },
    },
    "rerank": {
        "bge-reranker-v2-m3-q8_0.gguf": {
            "url": "https://huggingface.co/klnstpr/bge-reranker-v2-m3-Q8_0-GGUF/resolve/main/bge-reranker-v2-m3-q8_0.gguf",
            "min_bytes": 300_000_000,
            "size_hint": "~0.6 ГБ",
            "desc": "BAAI bge-reranker-v2-m3",
        },
    },
}


# ==================== Пути ====================

def runtime_dir() -> Path:
    """Каталог общего рантайма: env → локальный appdata → home.

    Кросс-платформенно (те же пути вычисляет ensure_llama_runtime.sh/.ps1):
      Windows — %LLAMA_RUNTIME_DIR% или %LOCALAPPDATA%\\llama-runtime
      macOS   — ~/Library/Application Support/llama-runtime
      прочие  — ~/.local/share/llama-runtime (XDG)
    """
    env = os.getenv(RUNTIME_DIR_ENV, "").strip()
    if env:
        return Path(env)
    if sys.platform == "win32":
        base = os.getenv("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / DEFAULT_DIRNAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / DEFAULT_DIRNAME
    return Path.home() / ".local" / "share" / DEFAULT_DIRNAME


def bin_dir() -> Path:
    return runtime_dir() / "bin"


def models_dir(role: Optional[str] = None) -> Path:
    d = runtime_dir() / "models"
    return d / role if role else d


def version_file() -> Path:
    return runtime_dir() / "version.json"


def projects_file() -> Path:
    return runtime_dir() / "projects.json"


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001 — битый/отсутствующий файл = пусто
        return {}


# ==================== Бинарь и манифест чат-модели ====================

def find_binary() -> str:
    """llama-server из общего рантайма; "" — нет (проекты упадут на
    следующем звене своего автопоиска: tools/llama.cpp → PATH).

    На macOS/Homebrew bin\\llama-server обычно символическая ссылка на
    бинарь brew (так апгрейд llama.cpp подхватывается автоматически) —
    is_file() её разыменовывает, поэтому проверка одинакова на всех ОС.
    """
    exe = "llama-server.exe" if sys.platform == "win32" else "llama-server"
    p = bin_dir() / exe
    return str(p) if p.is_file() else ""


def install_hint() -> str:
    """Имя идемпотентного установщика рантайма для текущей ОС.

    Каталог в подсказке не указывается: в hermes-disk-search скрипт лежит
    в installers/, в anonymizer_proxy — в scripts/.
    """
    return ("ensure_llama_runtime.ps1" if sys.platform == "win32"
            else "ensure_llama_runtime.sh")


def version_info() -> dict:
    return _read_json(version_file())


def current_file(role: str = "chat") -> Path:
    return models_dir(role) / "current.json"


def read_current(role: str = "chat") -> dict:
    return _read_json(current_file(role))


def ensure_chat_manifest() -> Optional[str]:
    """Гарантировать манифест chat: если current.json нет/пуст, а в каталоге
    есть DEFAULT_CHAT или ровно один GGUF — создать. Имя файла или None."""
    d = models_dir("chat")
    cur = read_current("chat").get("file", "")
    if cur and (d / cur).is_file():
        return cur
    if (d / DEFAULT_CHAT).is_file():
        chosen = DEFAULT_CHAT
    else:
        ggufs = sorted(p.name for p in d.glob("*.gguf"))
        if len(ggufs) != 1:
            return None
        chosen = ggufs[0]
    set_current_chat(chosen)
    return chosen


def set_current_chat(filename: str) -> dict:
    """Переписать манифест активной чат-модели (атомарно)."""
    filename = os.path.basename((filename or "").strip())
    if not filename:
        raise ValueError("Имя файла модели пустое")
    d = models_dir("chat")
    if not (d / filename).is_file():
        raise FileNotFoundError(
            f"GGUF-файла нет в общем рантайме: {d / filename}. Скачайте "
            f"пресет (download_chat_model) или положите файл в {d}")
    data = {"file": filename, "switched_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    _atomic_write_json(current_file("chat"), data)
    return data


def resolve_model(spec: str, role: str = "chat") -> Path:
    """'shared:<role>' → путь GGUF из манифеста рантайма; иначе Path(spec).

    FileNotFoundError — манифеста/файла нет (вызывающий превращает это в
    понятную ошибку конфигурации).
    """
    s = (spec or "").strip()
    if s == SHARED_PREFIX.rstrip(":"):     # «shared» без роли — роль по умолчанию
        s = SHARED_PREFIX + role
    if not s.startswith(SHARED_PREFIX):
        return Path(s)
    r = s[len(SHARED_PREFIX):].strip() or role
    d = models_dir(r)
    name = read_current(r).get("file", "")
    if name:
        p = d / name
        if p.is_file():
            return p
        # имя в манифесте могло отличаться регистром (файловая система
        # Windows регистронезависима, манифест — нет)
        for cand in d.glob("*.gguf"):
            if cand.name.lower() == name.lower():
                return cand
        raise FileNotFoundError(
            f"Модель shared:{r} не найдена в общем рантайме: манифест "
            f"current.json указывает на {name}, файла нет в {d}. Выполните "
            f"switch_chat_model(<файл>) или {install_hint()}")
    ggufs = sorted(d.glob("*.gguf"))
    if len(ggufs) == 1:
        # единственная модель роли = активная (манифест ещё не создан)
        return ggufs[0]
    raise FileNotFoundError(
        f"Модель shared:{r} не найдена в общем рантайме {d}: манифест "
        f"current.json битый или отсутствует, моделей в каталоге: "
        f"{len(ggufs)}. Выполните switch_chat_model(<файл>) или "
        f"{install_hint()}")



# ==================== Реестр проектов ====================

def list_projects() -> list:
    data = _read_json(projects_file())
    items = data.get("projects") if isinstance(data, dict) else data
    return [p for p in (items or []) if isinstance(p, dict) and p.get("root")]


def register_project(name: str, root: str, restart_args: list) -> None:
    """Добавить/обновить проект в реестре (вызывают установщики)."""
    root = str(Path(root).resolve())
    items = [p for p in list_projects() if p.get("name") != name
             and Path(p.get("root", "")).resolve() != Path(root)]
    items.append({"name": name, "root": root,
                  "restart_args": [str(a) for a in restart_args]})
    _atomic_write_json(projects_file(), {"projects": items})


# ==================== Скачивание ====================

def _human(n: float) -> str:
    for unit in ("Б", "МиБ", "ГиБ"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ТиБ"


def download_model(role: str, filename: str,
                   progress: Optional[Callable[[float, str], None]] = None) -> Path:
    """Скачать GGUF-пресет роли в общий рантайм (.part → атомарный rename)."""
    preset = (ROLE_PRESETS.get(role) or {}).get(filename) \
        or (CHAT_PRESETS if role == "chat" else {}).get(filename)
    if not preset:
        raise ValueError(f"Неизвестный пресет: {filename} (роли {role})")
    dest = models_dir(role) / filename
    if dest.is_file() and dest.stat().st_size >= preset["min_bytes"]:
        return dest  # идемпотентно
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(preset["url"],
                                 headers={"User-Agent": "llama-runtime"})
    token = os.getenv("HF_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if progress:
        progress(0.0, f"загрузка {filename} ({preset['size_hint']})")
    with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as f:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if progress:
                pct = (100.0 * done / total) if total else 0.0
                progress(pct, f"{_human(done)} / {_human(total or done)}")
    if tmp.stat().st_size < preset["min_bytes"]:
        tmp.unlink(missing_ok=True)
        raise OSError(f"Файл недокачан: {tmp.stat().st_size} байт")
    os.replace(tmp, dest)
    if progress:
        progress(100.0, f"готово: {dest}")
    return dest


def download_chat_model(filename: str,
                        progress: Optional[Callable[[float, str], None]] = None) -> Path:
    return download_model("chat", filename, progress)

# ==================== Смена модели ====================

def _restart_entry(entry: dict, timeout: float = 300.0) -> dict:
    """Перезапуск llama-инстанса зарегистрированного проекта (subprocess)."""
    root = Path(entry["root"])
    if sys.platform == "win32":
        py = root / ".venv" / "Scripts" / "python.exe"
    else:
        py = root / ".venv" / "bin" / "python"
    args = entry.get("restart_args") or []
    if not py.is_file() or not args:
        return {"name": entry.get("name", "?"), "root": str(root),
                "ok": False,
                "msg": "не найден .venv python или restart_args"}
    try:
        res = subprocess.run([str(py)] + args, cwd=str(root),
                             capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"name": entry.get("name", "?"), "root": str(root),
                "ok": False, "msg": f"таймаут {timeout}s"}
    except OSError as exc:
        return {"name": entry.get("name", "?"), "root": str(root),
                "ok": False, "msg": str(exc)}
    tail = ((res.stdout or "") + (res.stderr or "")).strip().splitlines()
    return {"name": entry.get("name", "?"), "root": str(root),
            "ok": res.returncode == 0,
            "msg": (tail[-1] if tail else
                    ("OK" if res.returncode == 0 else f"код {res.returncode}"))}


def switch_chat_model(filename: str,
                      restart: bool = True,
                      self_root: Optional[str] = None,
                      self_restart: Optional[Callable[[], dict]] = None,
                      download: bool = True,
                      progress: Optional[Callable[[float, str], None]] = None) -> dict:
    """Сменить активную чат-модель и перезапустить llama-инстансы проектов.

    1. Файла нет и это известный пресет → скачать (если download).
    2. Переписать models/chat/current.json.
    3. Перезапустить llama-инстансы всех зарегистрированных проектов;
       проект с root == self_root перезапускается через self_restart
       (in-process, своим менеджером), остальные — subprocess'ом
       (python .venv -m … restart, аргументы из projects.json).
    """
    filename = os.path.basename((filename or "").strip())
    preset = CHAT_PRESETS.get(filename)
    p = models_dir("chat") / filename
    if preset and not (p.is_file() and p.stat().st_size >= preset["min_bytes"]):
        if not download:
            return {"ok": False, "need_download": True,
                    "msg": f"Модель {filename} не скачана ({preset['size_hint']})"}
        try:
            download_model("chat", filename, progress)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "msg": f"Не удалось скачать {filename}: {exc}"}
    try:
        set_current_chat(filename)
    except (ValueError, FileNotFoundError) as exc:
        return {"ok": False, "msg": str(exc)}

    result = {"ok": True, "file": filename, "manifest": read_current("chat")}
    if restart:
        self_root = str(Path(self_root).resolve()) if self_root else None
        applied = []
        for entry in list_projects():
            is_self = self_root and \
                str(Path(entry["root"]).resolve()) == self_root
            if is_self and self_restart is not None:
                try:
                    r = self_restart()
                except Exception as exc:  # noqa: BLE001
                    r = {"ok": False, "msg": str(exc)}
                applied.append({"name": entry.get("name", "?"),
                                "root": entry["root"],
                                "ok": bool(r.get("ok")),
                                "msg": r.get("msg") or ""})
            else:
                applied.append(_restart_entry(entry))
        result["applied"] = applied
        result["ok"] = all(a.get("ok") for a in applied) if applied else True
    return result


# ==================== Обзор для UI ====================

def chat_models_overview() -> dict:
    """Всё, что нужно виджету «Чат-модель»: файлы на диске, пресеты,
    текущая модель, путь рантайма и статус бинаря."""
    d = models_dir("chat")
    available = [{"file": p.name, "size": p.stat().st_size,
                  "size_human": _human(p.stat().st_size)}
                 for p in sorted(d.glob("*.gguf"))] if d.is_dir() else []
    have = {a["file"] for a in available}
    presets = [{"file": name, "downloaded": name in have, **meta}
               for name, meta in CHAT_PRESETS.items()]
    return {
        "runtime": str(runtime_dir()),
        "models_dir": str(d),
        "current": read_current("chat").get("file", ""),
        "available": available,
        "presets": presets,
        "binary": find_binary(),
        "binary_ok": bool(find_binary()),
        "version": version_info(),
        "projects": [{"name": p.get("name", "?"), "root": p.get("root", "")}
                     for p in list_projects()],
    }


# ==================== CLI ====================

def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(
        prog="llama_runtime",
        description="Общий llama-рантайм машины: модели и llama-server")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="обзор рантайма (модели, бинарь, проекты)")
    sub.add_parser("dir", help="пути рантайма")
    sp = sub.add_parser("switch", help="сменить чат-модель (+перезапуск)")
    sp.add_argument("file", help="имя GGUF в models/chat или пресет")
    sp.add_argument("--no-restart", action="store_true",
                    help="только переписать манифест")
    sp.add_argument("--no-download", action="store_true",
                    help="не скачивать отсутствующий пресет")
    sp = sub.add_parser("download", help="скачать пресет модели")
    sp.add_argument("role", choices=["chat", "embedding", "rerank"])
    sp.add_argument("file", help="имя файла-пресета")
    sp = sub.add_parser("register", help="зарегистрировать проект")
    sp.add_argument("name")
    sp.add_argument("root")
    sp.add_argument("restart_args", nargs="+",
                    help="аргументы рестарта, напр.: -m pkg.llm_server restart")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if args.command == "dir":
        print(json.dumps({"runtime": str(runtime_dir()),
                          "bin": str(bin_dir()),
                          "models": str(models_dir()),
                          "chat": str(models_dir("chat"))},
                         ensure_ascii=False, indent=2))
        return 0
    if args.command == "list":
        print(json.dumps(chat_models_overview(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "switch":
        info = switch_chat_model(args.file, restart=not args.no_restart,
                                 download=not args.no_download)
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0 if info.get("ok") else 1
    if args.command == "download":
        try:
            p = download_model(args.role, args.file,
                               progress=lambda pct, msg:
                               print(f"\r  {pct:5.1f}%  {msg}", end=""))
        except Exception as exc:  # noqa: BLE001
            print(f"\n[ОШИБКА] {exc}", file=sys.stderr)
            return 1
        print(f"\nГотово: {p}")
        return 0
    if args.command == "register":
        register_project(args.name, args.root, args.restart_args)
        print(f"Зарегистрирован проект {args.name} ({args.root})")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())

