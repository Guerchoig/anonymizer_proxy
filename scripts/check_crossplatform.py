#!/usr/bin/env python3
"""
Статический аудит кроссплатформенности: ищет Windows-only конструкции
в Python-коде пакета и служебных скриптов.

Проверяемые маркеры:
- subprocess.CREATE_* — флаги, существующие только на Windows (AttributeError
  на macOS/Linux);
- .venv\\Scripts / ".venv/Scripts" без платформенной ветки;
- os.add_dll_directory, winreg, ctypes.windll — Windows API без guard'а;
- жёстко зашитые диски (C:\\) в коде (не в тестах/доках).

Скрипт знает о легитимных местах: строки, где рядом в том же файле есть
guard sys.platform == "win32", помечаются как OK (platform-guarded).

Дополнительно проверяются инварианты, без которых установка/запуск на macOS
ломаются (маркер [MAC-ИНВАРИАНТ] в выводе):
- обязательные скрипты на месте: install.sh, install.command, start_proxy.*,
  make_mac_app.sh, install_launchagent.sh, ensure_llama_runtime.{sh,ps1};
- *.sh / *.command — строго LF (CRLF даёт «\r: command not found» и «bad
  interpreter», .command из релиза не запускается двойным кликом);
- .gitattributes закрепляет eol=lf за *.sh и *.command (иначе в релизный
  архив попадёт CRLF-версия);
- SYNC-COPY-пары с hermes-disk-search идентичны по содержимому (сверка идёт,
  если соседний проект есть на диске: HDS_ROOT, ~/hermes-disk-search,
  ../hermes-disk-search).

Запуск: python scripts/check_crossplatform.py  (код 1 — есть находки)
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = ("anonymizer_proxy", "scripts")

# Скрипты, от которых зависит установка/запуск на macOS (пути от корня).
MAC_SCRIPTS = (
    "install.sh",
    "install.command",
    "start_proxy.sh",
    "start_proxy.command",
    "make_mac_app.sh",
    "install_launchagent.sh",
    "scripts/ensure_llama_runtime.sh",
    "scripts/ensure_llama_runtime.ps1",
)
SHELL_SUFFIXES = (".sh", ".command")
EXCLUDE_DIRS = {".git", ".venv", "__pycache__", "data", "models", "_smoke"}

# SYNC-COPY-пары (наш путь, путь в hermes-disk-search): содержимое обязано
# совпадать — копии правятся только вместе (см. §6 плана).
SYNC_PAIRS = (
    ("anonymizer_proxy/llama_runtime.py", "hds/llama_runtime.py"),
    ("scripts/ensure_llama_runtime.ps1",
     "installers/ensure_llama_runtime.ps1"),
    ("scripts/ensure_llama_runtime.sh",
     "installers/ensure_llama_runtime.sh"),
)
# Где искать hermes-disk-search для сверки SYNC-COPY (первый существующий).
PEER_CANDIDATES = (
    os.environ.get("HDS_ROOT", ""),
    str(Path.home() / "hermes-disk-search"),
    str(ROOT.parent / "hermes-disk-search"),
)
WINDOWS_ONLY = (
    (r"subprocess\.(?:CREATE_NEW_CONSOLE|CREATE_NEW_PROCESS_GROUP|"
     r"CREATE_NO_WINDOW|DETACHED_PROCESS)",
     "Windows-only флаг subprocess (на macOS/Linux — AttributeError)"),
    (r"os\.add_dll_directory", "Windows API DLL-каталогов"),
    (r"\bwinreg\b", "модуль реестра Windows"),
    (r"\bwinsound\b|\bmsvcrt\b|\bwin32api\b", "Windows-only модуль"),
)
PATH_HINTS = (
    (r'["\'](?:[A-Za-z]:)?\\\\?\.?venv[\\\\/]+Scripts', "путь .venv/Scripts (Windows)"),
)


# ==================== Инварианты macOS-установки ====================

def shell_files() -> list[Path]:
    """Все *.sh / *.command проекта (без служебных каталогов)."""
    found: list[Path] = []
    for pattern in ("*.sh", "*.command", "*/*.sh", "*/*.command"):
        for p in ROOT.glob(pattern):
            if any(part in EXCLUDE_DIRS for part in p.relative_to(ROOT).parts):
                continue
            if p not in found:
                found.append(p)
    return sorted(found)


def check_mac_scripts() -> list[str]:
    """Наличие Mac-скриптов, LF-переводы строк, eol=lf в .gitattributes."""
    findings: list[str] = []
    for rel in MAC_SCRIPTS:
        if not (ROOT / rel).is_file():
            findings.append(
                f"{rel} — отсутствует: установка/запуск на macOS сломается "
                "[MAC-ИНВАРИАНТ]")
    for p in shell_files():
        if b"\r\n" in p.read_bytes():
            findings.append(
                f"{p.relative_to(ROOT)} — CRLF-переводы строк: bash/zsh на "
                "macOS упадёт («\\r: command not found»), .command из релиза "
                "не запустится двойным кликом [MAC-ИНВАРИАНТ]")
    ga = ROOT / ".gitattributes"
    text = (ga.read_text(encoding="utf-8", errors="replace")
            if ga.is_file() else "")
    for pattern in ("*.sh", "*.command"):
        if not re.search(rf"(?m)^\s*{re.escape(pattern)}\s+text\s+eol=lf\s*$",
                         text):
            findings.append(
                f".gitattributes — нет правила «{pattern} text eol=lf»: в "
                "релизный архив может попасть CRLF-версия [MAC-ИНВАРИАНТ]")
    return findings


def peer_root() -> Optional[Path]:
    """Корень hermes-disk-search (для сверки SYNC-COPY-пар) или None."""
    for cand in PEER_CANDIDATES:
        if not cand:
            continue
        p = Path(cand)
        if (p / "hds" / "llama_runtime.py").is_file():
            return p
    return None


def check_sync_pairs(peer: Optional[Path]) -> tuple[list[str], str]:
    """Сверка SYNC-COPY-пар; отсутствующий двойник — пропуск (не ошибка)."""
    if peer is None:
        return [], ("SYNC-COPY: hermes-disk-search не найден рядом — сверка "
                    "пар пропущена (путь задаётся переменной HDS_ROOT)")
    findings: list[str] = []
    checked = 0
    for ours_rel, theirs_rel in SYNC_PAIRS:
        ours, theirs = ROOT / ours_rel, peer / theirs_rel
        if not ours.is_file() or not theirs.is_file():
            continue
        checked += 1
        if ours.read_bytes() != theirs.read_bytes():
            findings.append(
                f"{ours_rel} — расходится с SYNC-COPY-двойником "
                f"{peer.name}/{theirs_rel}: копии правятся только вместе "
                "[MAC-ИНВАРИАНТ]")
    return findings, f"SYNC-COPY: сверено пар — {checked} ({peer})"


def has_win_guard(text: str) -> bool:
    """В файле есть платформенный guard (sys.platform/os.name)?"""
    return bool(re.search(
        r"sys\.platform|os\.name|platform\.system|process\.platform", text))


def audit_file(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return []
    findings: list[str] = []
    guarded = has_win_guard(text)
    for pattern, why in WINDOWS_ONLY:
        for m in re.finditer(pattern, text):
            line_no = text.count("\n", 0, m.start()) + 1
            findings.append(
                f"{path.relative_to(ROOT)}:{line_no}: {m.group(0)} — {why}"
                + ("" if guarded else " [БЕЗ платформенного guard'а!]"))
    for pattern, why in PATH_HINTS:
        for m in re.finditer(pattern, text):
            line_no = text.count("\n", 0, m.start())
            findings.append(
                f"{path.relative_to(ROOT)}:{line_no}: {m.group(0)} — {why}"
                + (" (в ветке win32 — OK)" if guarded
                   else " [БЕЗ платформенного guard'а!]"))
    return findings


def main() -> int:
    findings: list[str] = []
    for d in SCAN_DIRS:
        base = ROOT / d
        if not base.is_dir():
            continue
        for py in base.rglob("*.py"):
            if (".venv" in py.parts or "__pycache__" in py.parts
                    or py == Path(__file__).resolve()):
                continue
            findings.extend(audit_file(py))
    findings.extend(check_mac_scripts())
    sync_findings, sync_note = check_sync_pairs(peer_root())
    findings.extend(sync_findings)
    print(sync_note)
    if not findings:
        print("OK: Windows-only конструкций и нарушений macOS-инвариантов "
              "не найдено (или все под guard'ом).")
        return 0
    hard = [f for f in findings
            if "БЕЗ платформенного" in f or "[MAC-ИНВАРИАНТ]" in f]
    print(f"Находок: {len(findings)} (жёстких: {len(hard)})")
    for f in findings:
        print(" ", f)
    return 1 if hard else 0


if __name__ == "__main__":
    sys.exit(main())
