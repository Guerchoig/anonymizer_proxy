#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Дополняет существующий .env недостающими ключами из .env.example.

Зачем: установщик НЕ перезаписывает .env (настройки пользователя
сохраняются), но при обновлении поверх СТАРОЙ установки схема конфига
меняется. Практический случай: релиз перешёл с LM Studio на llama-server и
добавил секцию LLM_SERVER_*; на машине со старым .env этих ключей нет —
LLM_SERVER_MODEL пуст, поэтому прокси НЕ запускает llama-server, а
устаревший LOCAL_LLM_BASE_URL указывает на порт, где никто не слушает
(ответ 502 «llama-server не отвечает на http://127.0.0.1:8080/v1», хотя в
диспетчере задач живёт llama-server СОСЕДНЕГО проекта на другом порту).

Шаг идемпотентен: добавляет ТОЛЬКО отсутствующие ключи со значениями из
.env.example (существующие значения НЕ перезаписываются) и уводит в
комментарий устаревшие/конфликтующие переопределения локальной модели, а
также ДУБЛИ ключей (python-dotenv молча берёт последнее значение — строка
пользователя выше по файлу не действовала бы).

Запуск (обычно вызывается установщиком):
    python scripts/ensure_env_keys.py            # дополнить .env
    python scripts/ensure_env_keys.py --check    # только проверить (exit 1)
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_EXAMPLE = PROJECT_ROOT / ".env.example"
DEFAULT_ENV = PROJECT_ROOT / ".env"

_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")

# Ключи, удалённые из схемы (пережиток старой архитектуры): уводим в
# комментарий, чтобы не вводили в заблуждение при чтении .env.
OBSOLETE_KEYS = {
    "LOCAL_LLM_MIN_MAX_TOKENS":
        "ключ удалён (лимит задаётся LLM_SERVER_MAX_OUTPUT_TOKENS)",
}


# ==================== Разбор .env / .env.example ====================

def _split_lines(text: str) -> list[str]:
    """Строки без учёта типа переводов строк (CRLF/LF)."""
    return text.splitlines()


def example_keys(example_path: Path) -> dict[str, str]:
    """Незакомментированные KEY=value из .env.example — эталон схемы."""
    keys: dict[str, str] = {}
    for line in _split_lines(example_path.read_text(encoding="utf-8")):
        if line.lstrip().startswith("#"):
            continue
        m = _KEY_RE.match(line.strip())
        if m:
            keys[m.group(1)] = m.group(2)
    return keys


def env_keys(env_path: Path) -> set[str]:
    """Набор активных (не закомментированных) ключей .env."""
    keys: set[str] = set()
    for line in _split_lines(env_path.read_text(encoding="utf-8")):
        if line.lstrip().startswith("#"):
            continue
        m = _KEY_RE.match(line.strip())
        if m:
            keys.add(m.group(1))
    return keys


def _value_of(lines: list[str], key: str) -> str:
    """Значение активного ключа (кавычки снимаются); "" — не найден."""
    for line in lines:
        if line.lstrip().startswith("#"):
            continue
        m = _KEY_RE.match(line.strip())
        if m and m.group(1) == key:
            return m.group(2).strip().strip('"')
    return ""


def _comment_key(lines: list[str], key: str, reason: str) -> bool:
    """Закомментировать активный ключ с пояснением. True — был закомментирован."""
    for i, line in enumerate(lines):
        m = _KEY_RE.match(line.strip())
        if m and m.group(1) == key:
            lines[i] = f"# {key}={m.group(2)}  # {reason}"
            return True
    return False


def _port_of(url: str) -> int | None:
    try:
        return urlsplit(url.strip()).port
    except (ValueError, AttributeError):
        return None


def _dedupe_keys(lines: list[str]) -> list[str]:
    """Закомментировать дубли активных ключей (действует ПОСЛЕДНЕЕ значение).

    python-dotenv читает .env в dict и при дубле ключа молча берёт последнее
    значение, поэтому строка, добавленная ВЫШЕ, не действует. Реальный случай:
    пользователь поставил LLM_SERVER_PORT=8010 (общий инстанс hermes), а
    автодобавленная миграцией в конец строка 8080 «победила» — прокси ходил
    не на тот порт и пытался поднять второй llama-server. Приводим файл к
    однозначному виду и сообщаем, какие значения проигнорированы.
    """
    positions: dict[str, list[int]] = {}
    for i, line in enumerate(lines):
        m = _KEY_RE.match(line.strip())
        if m:
            positions.setdefault(m.group(1), []).append(i)
    warnings: list[str] = []
    for key, idxs in positions.items():
        if len(idxs) < 2:
            continue
        for i in idxs[:-1]:          # все вхождения, кроме последнего
            m = _KEY_RE.match(lines[i].strip())
            warnings.append(
                f"дубль ключа {key}: строка {i + 1} ({m.group(2)!r}) "
                f"игнорируется — действует последнее значение")
            lines[i] = (f"# {key}={m.group(2)}  # дубль ключа: действует "
                        f"ПОСЛЕДНЕЕ значение (см. .env.example)")
    return warnings


def _effective_llama_port(lines: list[str], example: dict[str, str],
                          added: list[str]) -> int:
    """Порт llama-server, который получится после миграции."""
    if "LLM_SERVER_PORT" in added:
        raw = example.get("LLM_SERVER_PORT", "8080")
    else:
        raw = (_value_of(lines, "LLM_SERVER_PORT")
               or example.get("LLM_SERVER_PORT", "8080"))
    return int(raw) if str(raw).strip().isdigit() else 8080

# ==================== Миграция ====================

def ensure_env(example_path: Path = DEFAULT_ENV_EXAMPLE,
               env_path: Path = DEFAULT_ENV,
               check: bool = False) -> tuple[list[str], list[str]]:
    """Дополнить .env по эталону. Возвращает (добавленные_ключи, предупреждения).

    check=True — ничего не пишем на диск (для самопроверки/CI).
    """
    if not example_path.is_file():
        raise FileNotFoundError(f"не найден эталон конфигурации: {example_path}")
    if not env_path.is_file():
        raise FileNotFoundError(f"не найден .env: {env_path}")

    example = example_keys(example_path)
    text = env_path.read_text(encoding="utf-8")
    newline = "\r\n" if "\r\n" in text else "\n"

    lines = _split_lines(text)
    present = env_keys(env_path)
    added = [k for k in example if k not in present]
    # 0. Дубли ключей -> в комментарий (python-dotenv молча берёт последнее
    #    значение, из-за чего строка пользователя выше по файлу не действует)
    warnings: list[str] = _dedupe_keys(lines)

    # 1. Устаревшие ключи -> в комментарий (значение пользователя сохраняем)
    for i, line in enumerate(lines):
        m = _KEY_RE.match(line.strip())
        if m and m.group(1) in OBSOLETE_KEYS:
            key, value = m.group(1), m.group(2)
            lines[i] = f"# {key}={value}  # устарело: {OBSOLETE_KEYS[key]}"
            warnings.append(f"устаревший ключ {key} закомментирован")

    # 2. Явный LOCAL_LLM_BASE_URL, указывающий НЕ на тот порт, что
    #    LLM_SERVER_PORT -> в комментарий: URL выводится из
    #    LLM_SERVER_HOST/PORT, а «залипший» порт давал 502 «сервер не отвечает».
    port = _effective_llama_port(lines, example, added)
    base_url = _value_of(lines, "LOCAL_LLM_BASE_URL")
    base_port = _port_of(base_url) if base_url else None
    if base_port is not None and base_port != port:
        if _comment_key(lines, "LOCAL_LLM_BASE_URL",
                        f"порт {base_port} != LLM_SERVER_PORT {port}; "
                        f"URL выводится из LLM_SERVER_HOST/PORT"):
            warnings.append(
                f"LOCAL_LLM_BASE_URL (порт {base_port}) конфликтовал с "
                f"LLM_SERVER_PORT={port} — закомментирован")

    # 3. Добавляем отсутствующие ключи со значениями эталона
    if added:
        lines += [
            "",
            "# ---- Добавлено установщиком: ключи, появившиеся после обновления",
            "# (значения взяты из .env.example — при необходимости правьте) ----",
        ]
        lines += [f"{k}={example[k]}" for k in added]

    updated = newline.join(lines) + newline
    if not check and updated != text:
        env_path.write_text(updated, encoding="utf-8")

    return added, warnings


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="ensure_env_keys",
        description="Дополняет .env недостающими ключами из .env.example "
                    "(миграция после обновления)")
    parser.add_argument("--check", action="store_true",
                        help="только проверить (exit 1, если ключи отсутствуют)")
    parser.add_argument("--env", default=str(DEFAULT_ENV),
                        help="путь к .env (по умолчанию в корне проекта)")
    parser.add_argument("--example", default=str(DEFAULT_ENV_EXAMPLE),
                        help="путь к .env.example (по умолчанию в корне проекта)")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    try:
        added, warnings = ensure_env(Path(args.example), Path(args.env),
                                     check=args.check)
    except FileNotFoundError as exc:
        print(f"    [ВНИМАНИЕ] {exc} — миграция .env пропущена")
        return 0  # не фейлим установку из-за отсутствия файла

    for warning in warnings:
        print(f"    [!] {warning}")

    if added:
        if args.check:
            print(f"    [ВНИМАНИЕ] В .env отсутствуют ключи из .env.example "
                  f"({len(added)}): {', '.join(added)}")
            print("    Дополните: python scripts/ensure_env_keys.py")
            return 1
        print(f"    OK: .env дополнен ключами из .env.example ({len(added)}): "
              f"{', '.join(added)}")
        return 0

    print("    OK: .env содержит все ключи из .env.example")
    return 0


if __name__ == "__main__":
    sys.exit(main())
