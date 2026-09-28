# configure_cline.py — подключить standalone-приложение Cline к anonymizer-proxy.
#
# Standalone-приложение Cline (клиент cline.bot, НЕ расширение VS Code) хранит
# провайдеров в обычном JSON-файле:
#   <домашняя папка>/.cline/data/settings/providers.json
# (Windows: C:\Users\<пользователь>\.cline\..., macOS: /Users/<пользователь>/.cline/...).
# Скрипт добавляет туда OpenAI-совместимого провайдера, указывающего на
# локальный прокси, минуя UI («Add provider» иногда не активируется, если
# не заполнены ВСЕ обязательные поля формы).
#
# Прокси игнорирует API-ключ и имя модели из клиента — реальные значения
# берёт из своего .env. Поэтому в Cline годится любое непустое значение.
#
# Запуск (Windows, из корня установки прокси):
#   .venv\Scripts\python.exe scripts\configure_cline.py
# Запуск (macOS/Linux):
#   .venv/bin/python scripts/configure_cline.py
#
# Скрипт требует ЗАКРЫТОГО приложения Cline: оно держит providers.json и
# при завершении перезаписывает файл своим состоянием (правка при живом
# приложении будет потеряна). Обход проверки — флаг --force.
#
# Повторный запуск идемпотентен: существующий профиль с тем же именем
# обновляется, остальные не трогаются; перед первой правкой создаётся
# резервная копия providers.json.bak-<метка времени>.
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

PROVIDER_TYPE = "openai-compatible"
DEFAULT_BASE_URL = "http://127.0.0.1:8081/v1"
DEFAULT_MODEL = "anonymizer-proxy"
DEFAULT_API_KEY = "sk-anonymizer-proxy"
DEFAULT_NAME = "openai-compatible"
DEFAULT_TIMEOUT_MS = 120000


def providers_file() -> Path:
    """Файл провайдеров standalone-приложения Cline (все ОС: ~/.cline/...)."""
    return Path.home() / ".cline" / "data" / "settings" / "providers.json"


def cline_running() -> list[str]:
    """Лучше-усилийная проверка: запущено ли standalone-приложение Cline."""
    procs: list[str] = []
    try:
        if sys.platform == "win32":
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq Cline.exe", "/NH"],
                capture_output=True, text=True, timeout=15,
            ).stdout
            if "Cline.exe" in out:
                procs.append("Cline.exe")
        else:
            for pattern in ("Cline", "cline"):
                out = subprocess.run(
                    ["pgrep", "-x", pattern],
                    capture_output=True, text=True, timeout=15,
                ).stdout
                if out.strip():
                    procs.append(pattern)
    except Exception as exc:  # noqa: BLE001 — проверка не должна ронять скрипт
        print(f"[ВНИМАНИЕ] Не удалось проверить запущенные процессы: {exc}")
    return procs


def check_proxy(base_url: str) -> None:
    """Проверить, что прокси отвечает (warning, но не блокировка)."""
    parts = urlsplit(base_url)
    health_url = f"{parts.scheme}://{parts.netloc}/health"
    try:
        with urllib.request.urlopen(health_url, timeout=5) as resp:  # noqa: S310
            data = json.loads(resp.read().decode("utf-8"))
        print(f"[OK] Прокси отвечает: {health_url} "
              f"(версия {data.get('version', '?')})")
    except Exception as exc:  # noqa: BLE001
        print(f"[ВНИМАНИЕ] Прокси не отвечает на {health_url}: {exc}")
        print("           Провайдер всё равно будет прописан — проверьте,")
        print("           что прокси запущен, когда соберётесь работать.")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


def build_entry(args: argparse.Namespace) -> dict:
    return {
        "settings": {
            "provider": PROVIDER_TYPE,
            "apiKey": args.api_key,
            "model": args.model,
            "baseUrl": args.base_url,
            "headers": {},
            "timeout": DEFAULT_TIMEOUT_MS,
        },
        "updatedAt": now_iso(),
        "tokenSource": "manual",
    }


def atomic_write(path: Path, data: dict) -> None:
    """Записать JSON атомарно: temp-файл в той же папке + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Подключить standalone-приложение Cline к anonymizer-proxy")
    parser.add_argument("--file", type=Path, default=None,
                        help=f"путь к providers.json "
                             f"(по умолчанию {providers_file()})")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL,
                        help=f"адрес прокси (по умолчанию {DEFAULT_BASE_URL})")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help=f"имя модели (по умолчанию {DEFAULT_MODEL})")
    parser.add_argument("--api-key", default=DEFAULT_API_KEY,
                        help="API-ключ (прокси его игнорирует — годится любое "
                             "непустое значение)")
    parser.add_argument("--name", default=DEFAULT_NAME,
                        help=f"имя профиля в Cline (по умолчанию {DEFAULT_NAME})")
    parser.add_argument("--set-active", action="store_true",
                        help="сделать провайдера используемым по умолчанию")
    parser.add_argument("--dry-run", action="store_true",
                        help="показать изменения, файл не менять")
    parser.add_argument("--force", action="store_true",
                        help="не прерываться, если приложение Cline запущено")
    args = parser.parse_args()

    target = args.file or providers_file()
    print(f"Файл провайдеров: {target}")

    running = cline_running()
    if running and not args.dry_run and not args.force:
        print("[ОШИБКА] Похоже, приложение Cline запущено "
              f"({', '.join(running)}).", file=sys.stderr)
        print("Закройте его: при завершении Cline перезаписывает "
              "providers.json", file=sys.stderr)
        print("своим состоянием, и правка будет потеряна.", file=sys.stderr)
        print("Обход проверки (на свой риск): --force", file=sys.stderr)
        return 2
    if running:
        print("[ВНИМАНИЕ] Cline запущен — правка при живом приложении может "
              "быть перезаписана (--dry-run или закройте Cline).")

    check_proxy(args.base_url)

    if target.is_file():
        data = json.loads(target.read_text(encoding="utf-8"))
    else:
        if not args.dry_run and not target.parent.exists():
            print(f"[ОШИБКА] Каталог {target.parent} не найден — standalone-"
                  "приложение Cline установлено? Запустите его хотя бы раз "
                  "или задайте --file вручную.", file=sys.stderr)
            return 1
        print("[ВНИМАНИЕ] providers.json не найден — будет создан заново.")
        data = {"version": 1, "lastUsedProvider": "cline", "modes": {},
                "providers": {}}

    providers = data.setdefault("providers", {})
    entry = build_entry(args)
    existed = args.name in providers
    providers[args.name] = entry
    if args.set_active:
        data["lastUsedProvider"] = args.name

    if args.dry_run:
        print("[DRY-RUN] Изменения НЕ записаны. Запись для "
              f"«{args.name}» ({'обновление' if existed else 'новая'}):")
        print(json.dumps({args.name: entry}, ensure_ascii=False, indent=2))
        if args.set_active:
            print(f"lastUsedProvider -> {args.name}")
        return 0

    if target.is_file():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = target.with_name(f"{target.name}.bak-{stamp}")
        shutil.copy2(target, backup)
        print(f"Резервная копия: {backup}")

    atomic_write(target, data)

    print()
    print("[OK] Готово. Профиль «%s» %s в %s" % (
        args.name, "обновлён" if existed else "добавлен", target))
    print()
    print("Дальше в приложении Cline:")
    print("  1. Запустите Cline (если был закрыт) и откройте "
          "Settings -> Providers.")
    print(f"  2. Выберите профиль «{args.name}»"
          + (" (он уже активен по умолчанию)." if args.set_active else "."))
    print("  3. В чате модель можно указывать как есть — прокси подставит")
    print("     свою модель и ключ из .env; в Cline значения условные.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
