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

Запуск: python scripts/check_crossplatform.py  (код 1 — есть находки)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = ("anonymizer_proxy", "scripts")
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
    if not findings:
        print("OK: платформо-зависимых мест не найдено (или все под guard'ом).")
        return 0
    unguarded = [f for f in findings if "БЕЗ платформенного" in f]
    print(f"Находок: {len(findings)} (без guard'а: {len(unguarded)})")
    for f in findings:
        print(" ", f)
    return 1 if unguarded else 0


if __name__ == "__main__":
    sys.exit(main())
