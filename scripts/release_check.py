"""Проверка чистоты релизного архива перед публикацией в GitHub Releases.

Архив не должен содержать секретов и данных с PII:
- .env — API-ключи;
- data/ — логи, БД маппингов, анонимизированные документы;
- .venv/ — виртуальное окружение;
- *.docx / *.xlsx / *.db / *.sqlite* — документы и БД (возможен PII).

Запуск: python3 scripts/release_check.py <путь-к-zip>
Код возврата 0 — архив чистый; 1 — найдены запрещённые файлы.
"""
from __future__ import annotations

import re
import sys
import zipfile

FORBIDDEN = [
    (re.compile(r"(^|/)\.env$"), ".env (секреты)"),
    (re.compile(r"(^|/)data(/|$)"), "data/ (PII)"),
    (re.compile(r"(^|/)\.venv(/|$)"), ".venv/ (окружение)"),
    (re.compile(r"\.(docx|xlsx|db|sqlite3?)$", re.IGNORECASE), "документы/БД (возможен PII)"),
]


def main() -> int:
    if len(sys.argv) != 2:
        print("Использование: python3 scripts/release_check.py <архив.zip>", file=sys.stderr)
        return 1

    path = sys.argv[1]
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()

    bad = [
        (name, label)
        for name in names
        for pattern, label in FORBIDDEN
        if pattern.search(name)
    ]
    if bad:
        print("В архив попали запрещённые файлы:", file=sys.stderr)
        for name, label in bad:
            print(f"  {name} ({label})", file=sys.stderr)
        return 1

    print(f"Проверка чистоты архива: OK ({len(names)} файлов)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
