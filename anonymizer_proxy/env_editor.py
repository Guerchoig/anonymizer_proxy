"""
Редактор .env через веб-форму: белый список ключей, хирургическая правка.

Ключевые решения безопасности:
- секреты (API-ключи, токены) write-only: наружу отдаётся только факт
  «задан / не задан», значение никогда не возвращается и не логируется;
- обновлять можно ТОЛЬКО ключи из белого списка schema() — произвольные
  переменные через форму записать нельзя;
- перед каждой записью создаётся бэкап в data/env_backups/
  (хранятся последние MAX_BACKUPS копий);
- сохраняется формат файла: UTF-8, наличие/отсутствие BOM и CRLF/LF
  определяются по текущему .env и воспроизводятся при записи;
- правка «хирургическая»: заменяется только строка нужного ключа —
  комментарии, порядок строк и прочие ключи не трогаются.

Все функции принимают env_path/backup_dir (для тестируемости).
"""
import re
import shutil
import time
from pathlib import Path
from typing import Optional

from . import config as _config
from .config import (
    BASE_DIR, CLOUD_PROVIDERS, DATA_DIR, RUNTIME, logger, save_runtime_state,
)

ENV_PATH = BASE_DIR / ".env"
BACKUP_DIR = DATA_DIR / "env_backups"
MAX_BACKUPS = 10


class EnvEditorError(ValueError):
    """Ошибка редактирования .env (невалидный ключ/значение/файл)."""


# ==================== Белый список редактируемых ключей ====================

# Целочисленные ключи: {KEY: (min, max)}
INT_KEYS: dict[str, tuple[int, int]] = {
    "PROXY_PORT": (1, 65535),
    "LOCAL_LLM_TIMEOUT": (1, 3600),
    "LLM_SERVER_PORT": (1, 65535),
    "LLM_SERVER_PARALLEL": (1, 32),
    "LLM_SERVER_CTX_PER_SLOT": (1024, 131072),
}
CHOICE_KEYS: dict[str, list[str]] = {}


def _key_env(name: str) -> str:
    """Env-переменная ключа API провайдера (совпадает с config.py)."""
    return ("OPENROUTER_API_KEY" if name == "openrouter"
            else f"{name.upper()}_API_KEY")


def _model_env(name: str) -> str:
    return ("OPENROUTER_MODEL" if name == "openrouter"
            else f"{name.upper()}_MODEL")


def _url_env(name: str) -> str:
    return ("OPENROUTER_BASE_URL" if name == "openrouter"
            else f"{name.upper()}_BASE_URL")


def schema() -> list[dict]:
    """Белый список редактируемых ключей с метаданными.

    Каждый элемент: {key, group, description, is_secret}.
    """
    CHOICE_KEYS["CLOUD_PROVIDER"] = sorted(CLOUD_PROVIDERS)
    items: list[dict] = []

    items.append({
        "key": "CLOUD_PROVIDER", "group": "Общие",
        "description": "Стартовый провайдер облака (действующий выбирается "
                       "переключением в форме и запоминается)",
        "is_secret": False, "choices": sorted(CLOUD_PROVIDERS),
    })

    for name, cfg in CLOUD_PROVIDERS.items():
        group = f"Провайдер: {name}"
        if name == "openrouter":
            group += " (дефолт)"
        items.append({
            "key": _key_env(name), "group": group,
            "description": "API-ключ провайдера (write-only: значение не "
                           "показывается, подхватывается при старте прокси)",
            "is_secret": True,
        })
        if name == "custom" or not cfg["base_url"]:
            items.append({
                "key": _url_env(name), "group": group,
                "description": "Base URL (OpenAI-совместимый endpoint)",
                "is_secret": False,
            })
        items.append({
            "key": _model_env(name), "group": group,
            "description": "Модель по умолчанию",
            "is_secret": False,
        })

    items += [
        {"key": "LLM_SERVER_BIN", "group": "Локальная модель (llama.cpp)",
         "description": "Путь к llama-server (пусто — автопоиск: общий "
                        "llama-рантайм, tools/llama.cpp, PATH, Homebrew)",
         "is_secret": False},
        {"key": "LLM_SERVER_MODEL", "group": "Локальная модель (llama.cpp)",
         "description": "GGUF-модель: shared:chat — активная модель общего "
                        "llama-рантайма (меняется в виджете «Общая чат-модель» "
                        "или командой llama_runtime switch); абсолютный путь "
                        "к .gguf — escape-hatch",
         "is_secret": False},
        {"key": "LLM_SERVER_HOST", "group": "Локальная модель (llama.cpp)",
         "description": "Адрес привязки llama-server", "is_secret": False},
        {"key": "LLM_SERVER_PORT", "group": "Локальная модель (llama.cpp)",
         "description": "Порт llama-server (дефолт llama.cpp — 8080)",
         "is_secret": False},
        {"key": "LLM_SERVER_PARALLEL", "group": "Локальная модель (llama.cpp)",
         "description": "Слотов конкурентности: 1 — запросы строго по "
                        "очереди, каждый получает полный контекст (дефолт "
                        "проекта)", "is_secret": False},
        {"key": "LLM_SERVER_CTX_PER_SLOT",
         "group": "Локальная модель (llama.cpp)",
         "description": "Контекст на один запрос, токенов (32K — файл "
                        "через прокси / RAG-поиск); общий буфер сервера = "
                        "PARALLEL × это значение", "is_secret": False},
        {"key": "LLM_SERVER_API_KEY", "group": "Локальная модель (llama.cpp)",
         "description": "Ключ llama-server (--api-key; пусто — без "
                        "авторизации, допустимо на localhost)",
         "is_secret": True},
        {"key": "LLM_SERVER_AUTOSTART",
         "group": "Локальная модель (llama.cpp)",
         "description": "Прокси при старте сам проверяет/запускает "
                        "llama-server (1/0)", "is_secret": False},
        {"key": "LLM_SERVER_EXTRA_ARGS",
         "group": "Локальная модель (llama.cpp)",
         "description": "Доп. флаги llama-server (например, --n-gpu-layers "
                        "99, квантование KV-кэша)", "is_secret": False},

        {"key": "LOCAL_LLM_BASE_URL", "group": "Локальная модель (llama.cpp)",
         "description": "Адрес OpenAI-совместимого сервера llama-server "
                        "(пусто — из LLM_SERVER_HOST/PORT)",
         "is_secret": False},
        {"key": "LOCAL_LLM_MODEL", "group": "Локальная модель (llama.cpp)",
         "description": "Имя модели llama-server (пусто — первая с сервера)",
         "is_secret": False},
        {"key": "LOCAL_LLM_API_KEY", "group": "Локальная модель (llama.cpp)",
         "description": "Ключ авторизации на llama-server (обычно не "
                        "требуется; приоритетнее LLM_SERVER_API_KEY)",
         "is_secret": True},
        {"key": "LOCAL_LLM_TIMEOUT", "group": "Локальная модель (llama.cpp)",
         "description": "Таймаут запроса, сек (покрывает ожидание в очереди "
                        "за чужим длинным запросом)", "is_secret": False},

        {"key": "PROXY_HOST", "group": "Прокси-сервер",
         "description": "Адрес привязки (127.0.0.1 — только эта машина)",
         "is_secret": False},
        {"key": "PROXY_PORT", "group": "Прокси-сервер",
         "description": "Порт прокси", "is_secret": False},
        {"key": "OPENROUTER_PROXY", "group": "Прокси-сервер",
         "description": "VPN-прокси ТОЛЬКО для OpenRouter (пусто = прямой "
                        "доступ; российские провайдеры ходят напрямую "
                        "независимо от этого значения)",
         "is_secret": False},
        {"key": "PROXY_API_TOKEN", "group": "Прокси-сервер",
         "description": "Токен доступа к /api/* и к этой форме. После смены "
                        "и перезапуска войдите в форму заново с новым токеном!",
         "is_secret": True},
    ]
    return items


# ==================== Чтение/запись .env (с сохранением формата) ====================

_KEY_LINE_RE = re.compile(
    r"^\s*(?P<comment>#\s*)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=(?P<rest>.*)$")


def _read_env(env_path: Path) -> tuple[list[str], bool, str]:
    """Прочитать .env: (строки, bom, eol). FileNotFoundError — наружу."""
    data = env_path.read_bytes()
    bom = data.startswith(b"\xef\xbb\xbf")
    text = data.decode("utf-8-sig")
    eol = "\r\n" if "\r\n" in text else "\n"
    return text.split(eol), bom, eol


def _write_env(env_path: Path, lines: list[str], bom: bool, eol: str) -> None:
    text = eol.join(lines)
    data = text.encode("utf-8")
    env_path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + data)
    # POSIX: ограничить доступ к файлу с секретами (на Windows no-op)
    try:
        env_path.chmod(0o600)
    except OSError:  # noqa: BLE001 — не критично (напр. FAT-раздел)
        pass


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _find(lines: list[str], key: str) -> tuple[Optional[int], Optional[re.Match]]:
    """Найти строку ключа (активную или закомментированную)."""
    for idx, line in enumerate(lines):
        m = _KEY_LINE_RE.match(line)
        if m and m.group("key") == key:
            return idx, m
    return None, None


def _format_value(value: str) -> str:
    if value == "":
        return ""
    if re.search(r"[\s#]", value):
        return f'"{value}"'
    return value


def line_key_value(line: str) -> str:
    """«KEY=значение» из строки файла (без ведущих пробелов и «#»)."""
    return line.strip().removeprefix("#").strip()


# ==================== Схема с маскировкой секретов ====================

def _mask_secret(raw: str) -> str:
    """Маска ключа: первые 3 и последние 4 символа («sk-…7890»).

    Полное значение никогда не покидает сервер — пользователь видит
    достаточно, чтобы опознать ключ, но не может его украсть.
    """
    raw = raw.strip()
    if len(raw) <= 8:
        return "•" * len(raw)
    return f"{raw[:3]}…{raw[-4:]}"


def read_schema(env_path: Path = ENV_PATH) -> dict:
    """Текущее состояние .env по белому списку.

    Секреты НЕ возвращаются в полном виде: только has_value (задан/не
    задан) и маска вида «sk-…7890».
    """
    if not env_path.is_file():
        raise FileNotFoundError(f"Файл не найден: {env_path}")
    lines, _, _ = _read_env(env_path)
    items = []
    for meta in schema():
        idx, m = _find(lines, meta["key"])
        active = idx is not None and not m.group("comment")
        raw = _unquote(m.group("rest")) if idx is not None else ""
        entry = dict(meta)
        entry["active"] = active
        if meta["is_secret"]:
            entry["has_value"] = active and bool(raw) and \
                "REPLACE_WITH" not in raw.upper()
            entry["masked"] = _mask_secret(raw) if entry["has_value"] else None
            entry["value"] = None
        else:
            entry["has_value"] = active and bool(raw)
            entry["value"] = raw if active else None
        items.append(entry)
    return {"env_path": str(env_path), "items": items}


# ==================== Валидация значений ====================

def validate_value(meta: dict, value: Optional[str]) -> Optional[str]:
    """Вернуть текст ошибки или None (null = удаление — всегда валиден)."""
    if value is None:
        return None
    if not isinstance(value, str):
        return "значение должно быть строкой"
    if "\n" in value or "\r" in value:
        return "переводы строк в значении запрещены"
    if '"' in value:
        return "двойные кавычки в значении запрещены"
    key = meta["key"]
    if key in INT_KEYS:
        try:
            iv = int(value)
        except ValueError:
            return "ожидается целое число"
        lo, hi = INT_KEYS[key]
        if not lo <= iv <= hi:
            return f"ожидается число от {lo} до {hi}"
        return None
    if key in CHOICE_KEYS and value not in CHOICE_KEYS[key]:
        return f"допустимые значения: {', '.join(CHOICE_KEYS[key])}"
    if meta["is_secret"]:
        if not value.strip():
            return "пустое значение — для удаления передайте null"
        if "'" in value or value.strip() != value:
            return "ключ не должен содержать кавычек или пробелов по краям"
    return None


# ==================== Бэкапы ====================

def _make_backup(env_path: Path, backup_dir: Path) -> Path:
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = backup_dir / f".env.bak-{stamp}"
        shutil.copy2(env_path, backup)
        # храним только последние MAX_BACKUPS копий
        backups = sorted(backup_dir.glob(".env.bak-*"))
        for old in backups[:-MAX_BACKUPS]:
            old.unlink(missing_ok=True)
        return backup
    except OSError as exc:
        raise EnvEditorError(
            f"Не удалось создать бэкап .env — запись отменена: {exc}") from exc


# ==================== Хирургическое обновление ====================

def apply_updates(
    updates: dict, env_path: Path = ENV_PATH, backup_dir: Path = BACKUP_DIR,
) -> dict:
    """Обновить ключи .env. updates: {KEY: "значение" | None}.

    None — закомментировать строку ключа (с сохранением прежнего значения).
    Возвращает отчёт: changed/added/removed/backup/restart_required.
    """
    if not env_path.is_file():
        raise EnvEditorError(f"Файл .env не найден: {env_path}")

    meta_by_key = {m["key"]: m for m in schema()}
    clean: dict[str, Optional[str]] = {}
    for key, value in (updates or {}).items():
        if key not in meta_by_key:
            raise EnvEditorError(
                f"Ключ {key!r} не входит в белый список редактора настроек")
        if value is not None and not isinstance(value, str):
            raise EnvEditorError(f"{key}: значение должно быть строкой или null")
        err = validate_value(meta_by_key[key], value)
        if err:
            raise EnvEditorError(f"{key}: {err}")
        clean[key] = value.strip() if isinstance(value, str) else None

    if not clean:
        raise EnvEditorError("Пустой список изменений (updates)")

    lines, bom, eol = _read_env(env_path)
    backup = _make_backup(env_path, backup_dir)

    changed: list[str] = []
    added: list[str] = []
    removed: list[str] = []

    for key, value in clean.items():
        idx, m = _find(lines, key)
        if value is None:
            if idx is None or m.group("comment"):
                continue  # нечего удалять — не считаем ошибкой
            lines[idx] = f"# {line_key_value(lines[idx])}"
            removed.append(key)
            continue
        new_line = f"{key}={_format_value(value)}"
        if idx is None:
            lines.append(new_line)
            added.append(key)
        elif m.group("comment"):
            lines[idx] = new_line
            added.append(key)
        elif _unquote(m.group("rest")).strip() == value.strip():
            continue  # значение не изменилось
        else:
            lines[idx] = new_line
            changed.append(key)

    _write_env(env_path, lines, bom, eol)
    _sync_registry(lines, clean)
    logger.info(
        "ENV-редактор: изменено=%s, включено/добавлено=%s, закомментировано=%s "
        "(бэкап: %s)", changed or "-", added or "-", removed or "-", backup)
    return {
        "changed": changed,
        "added": added,
        "removed": removed,
        "backup": str(backup),
        "restart_required": True,
    }


def _sync_registry(lines: list[str], updated: dict) -> None:
    """Отразить правки .env в РАБОТАЮЩЕМ процессе (без перезапуска).

    Ключи/модели/base_url провайдеров пишутся в реестр CLOUD_PROVIDERS —
    клиенты читают их динамически. Смена CLOUD_PROVIDER из формы делает
    провайдера действующим (RUNTIME + runtime_state.json). Настройки
    PROXY_*/LOCAL_LLM_* применяются только после перезапуска.
    """
    for name, pcfg in CLOUD_PROVIDERS.items():
        for env_name, field in (
            (_key_env(name), "api_key"),
            (_url_env(name), "base_url"),
            (_model_env(name), "model"),
        ):
            if env_name not in updated:
                continue
            idx, m = _find(lines, env_name)
            if idx is None or m.group("comment"):
                if field == "api_key":
                    pcfg[field] = ""  # ключ закомментировали
                continue
            pcfg[field] = _unquote(m.group("rest")).strip()

    if "CLOUD_PROVIDER" in updated:
        idx, m = _find(lines, "CLOUD_PROVIDER")
        if idx is not None and not m.group("comment"):
            value = _unquote(m.group("rest")).strip()
            if value in CLOUD_PROVIDERS:
                # явный выбор в форме — провайдер становится действующим
                _config.CLOUD_PROVIDER = value
                RUNTIME["cloud"] = value
                RUNTIME["backend"] = value
                save_runtime_state()
                logger.info("Действующий облачный провайдер: %s", value)
