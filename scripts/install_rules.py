# install_rules.py — установить правила анонимизации в папку проекта.
#
# Правила агентов (Hermes Agent / Cline) берутся ИЗ ПАПКИ ПРОЕКТА, а не из
# глобальных хранилищ:
#   - Hermes Agent: <проект>/AGENTS.md — загружается автоматически в каждую
#     сессию (приоритет: .hermes.md -> AGENTS.override.md -> AGENTS.md);
#   - Cline: <проект>/.clinerules — читается автоматически, когда папка
#     проекта открыта в VS Code.
#
# Глобальные хранилища больше НЕ используются: у MEMORY.md лимит 2200
# символов — правила обрежутся; глобальные правила Cline действуют и там,
# где документами не занимаются.
#
# Использование (из корня установки прокси):
#   .venv\Scripts\python.exe scripts\install_rules.py <путь-к-проекту>
#       [--clinerules] [--force]
#
# Что делает:
#   1. Пишет <проект>/AGENTS.md — правила Hermes из шаблона hermes_rules.md
#      с подстановкой ФАКТИЧЕСКОГО пути установки прокси (в шаблоне
#      захардкожен путь исходной установки).
#   2. С --clinerules — дополнительно копирует .clinerules в проект
#      (для Cline в папках вне корня прокси).
#   3. Существующий файл не перезаписывает без --force.
import argparse
import re
import sys
from pathlib import Path

PROXY_ROOT = Path(__file__).resolve().parent.parent
# Шаблоны лежат рядом со скриптом (в установке прокси это один и тот же
# корень; константа независимая, чтобы подстановку пути можно было
# тестировать отдельно от расположения шаблонов)
TEMPLATE = Path(__file__).resolve().parent.parent / "hermes_rules.md"
CLINERULES = Path(__file__).resolve().parent.parent / ".clinerules"

# Пути-заглушки в шаблоне hermes_rules.md (путь исходной установки)
HARDCODED_PATHS = (
    r"C:\Test\anonymizer_proxy",
    r"C:\Test\anonymizer_proxy\\".replace("\\\\", "\\"),
    "C:/Test/anonymizer_proxy",
)


def render_agents_md() -> str:
    """Содержимое AGENTS.md: hermes_rules.md с фактическим путём прокси."""
    if not TEMPLATE.is_file():
        raise SystemExit(f"Шаблон не найден: {TEMPLATE}")
    text = TEMPLATE.read_text(encoding="utf-8")
    actual = str(PROXY_ROOT)
    for hardcoded in HARDCODED_PATHS:
        text = text.replace(hardcoded, actual)
    # Маркер источника: чтобы файл в проекте ссылался на установку прокси
    text = text.replace(
        "Источник-оригинал — `.clinerules`\n",
        f"Источник-оригинал — `.clinerules` (установка прокси: {actual})\n",
    )
    return text


def install(target_dir: Path, with_clinerules: bool, force: bool) -> int:
    target_dir = target_dir.resolve()
    if not target_dir.is_dir():
        raise SystemExit(f"Папка проекта не найдена: {target_dir}")

    changed = []

    agents = target_dir / "AGENTS.md"
    content = render_agents_md()
    if agents.is_file() and not force and agents.read_text(encoding="utf-8") != content:
        raise SystemExit(
            f"Файл уже существует и отличается: {agents}. "
            "Перезаписать его можно с флагом --force.")
    if not agents.is_file() or agents.read_text(encoding="utf-8") != content:
        agents.write_text(content, encoding="utf-8")
        changed.append(agents)

    if with_clinerules:
        dst = target_dir / ".clinerules"
        if not CLINERULES.is_file():
            raise SystemExit(f"Не найден шаблон .clinerules: {CLINERULES}")
        if dst.is_file() and not force and dst.read_text(encoding="utf-8") != CLINERULES.read_text(encoding="utf-8"):
            raise SystemExit(
                f"Файл уже существует и отличается: {dst}. "
                "Перезаписать его можно с флагом --force.")
        if not dst.is_file() or dst.read_text(encoding="utf-8") != CLINERULES.read_text(encoding="utf-8"):
            dst.write_text(CLINERULES.read_text(encoding="utf-8"), encoding="utf-8")
            changed.append(dst)

    if not changed:
        print(f"Правила уже актуальны: {target_dir}")
        return 0
    for p in changed:
        print(f"OK: записан {p}")
    print("Правила загружаются агентом автоматически: Hermes — из AGENTS.md "
          "(перезапуск чата не обязателен, новый чат точно подхватит); "
          "Cline — из .clinerules при открытой папке.")
    return 0


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        description="Установить правила анонимизации (AGENTS.md для Hermes, "
                    ".clinerules для Cline) в папку проекта.")
    parser.add_argument("project", help="папка проекта, в которой агент "
                        "работает с документами")
    parser.add_argument("--clinerules", action="store_true",
                        help="дополнительно записать .clinerules в проект "
                             "(для Cline)")
    parser.add_argument("--force", action="store_true",
                        help="перезаписать существующие файлы")
    args = parser.parse_args(argv)
    return install(Path(args.project), args.clinerules, args.force)


if __name__ == "__main__":
    raise SystemExit(main())
