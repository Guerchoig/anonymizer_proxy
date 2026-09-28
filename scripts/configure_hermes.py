# configure_hermes.py — подключить Hermes Agent (Nous Research) к
# anonymizer-proxy.
#
# Правит конфиг Hermes'а (config.yaml) так, как это описано в README
# («Подключение клиента 2»): добавляет/обновляет провайдера-прокси и
# переключает модель по умолчанию. Идемпотентно: остальные провайдеры и
# разделы конфига не трогаются; перед первой правкой создаётся резервная
# копия config.yaml.bak-<метка времени>.
#
# ВАЖНО: config.yaml перезаписывается через yaml.safe_dump — комментарии в
# нём не сохраняются (если комментарии дороги — откатитесь на .bak-копию и
# правьте блоки вручную по README).
#
# Запуск (из корня установки прокси):
#   Windows:  .venv\Scripts\python.exe scripts\configure_hermes.py
#   macOS:    .venv/bin/python scripts/configure_hermes.py
#
# Где Hermes ищется автоматически (первый существующий):
#   - $HERMES_CONFIG (явное переопределение);
#   - Windows: %LOCALAPPDATA%\hermes\config.yaml (десктоп-установщик);
#   - ~/.hermes/config.yaml (CLI/прочие варианты установки);
#   - macOS: ~/Library/Application Support/hermes/config.yaml.
# Путь можно задать и флагом --file.
#
# Скрипт НЕ настраивает fallback_model (по README облачный фоллбэк
# недопустим: при сбое прокси запросы молча уйдут в облако в обход
# анонимизации). Если fallback_model уже активен — печатается предупреждение.
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover — в .venv прокси pyyaml всегда есть
    print("[ОШИБКА] Не найден PyYAML. Запускайте скрипт через venv прокси:",
          file=sys.stderr)
    print("  Windows:  .venv\\Scripts\\python.exe scripts\\configure_hermes.py",
          file=sys.stderr)
    print("  macOS:    .venv/bin/python scripts/configure_hermes.py",
          file=sys.stderr)
    raise SystemExit(1)

PROVIDER_NAME = "anonymizer_proxy"
DEFAULT_BASE_URL = "http://127.0.0.1:8081/v1"
DEFAULT_MODEL = "anonymizer-proxy"
DEFAULT_CONTEXT_LENGTH = 128000


def config_candidates() -> list[Path]:
    """Кандидаты config.yaml в порядке приоритета (все ОС)."""
    cands: list[Path] = []
    env_cfg = os.environ.get("HERMES_CONFIG", "")
    if env_cfg:
        cands.append(Path(env_cfg))
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            cands.append(Path(local) / "hermes" / "config.yaml")
    cands += [
        Path.home() / ".hermes" / "config.yaml",
        Path.home() / "Library" / "Application Support" / "hermes" / "config.yaml",
    ]
    return cands


def find_config() -> Path | None:
    """Первый существующий config.yaml или None (Hermes не найден)."""
    for cand in config_candidates():
        if cand.is_file():
            return cand
    return None


def hermes_running() -> list[str]:
    """Лучше-усилийная проверка: запущен ли Hermes (десктоп/CLI)."""
    procs: list[str] = []
    try:
        if sys.platform == "win32":
            for exe in ("Hermes.exe", "hermes.exe"):
                out = subprocess.run(
                    ["tasklist", "/FI", f"IMAGENAME eq {exe}", "/NH"],
                    capture_output=True, text=True, timeout=15,
                ).stdout
                if exe in out:
                    procs.append(exe)
        else:
            for pattern in ("Hermes", "hermes"):
                out = subprocess.run(
                    ["pgrep", "-x", pattern],
                    capture_output=True, text=True, timeout=15,
                ).stdout
                if out.strip():
                    procs.append(pattern)
    except Exception as exc:  # noqa: BLE001 — проверка не должна ронять скрипт
        print(f"[ВНИМАНИЕ] Не удалось проверить запущенные процессы: {exc}")
    return procs


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


def atomic_write(path: Path, data: dict) -> None:
    """Записать YAML атомарно: temp-файл в той же папке + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False,
                           default_flow_style=False)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def build_provider_block(base_url: str, model: str,
                         context_length: int) -> dict:
    """Блок providers.<PROVIDER_NAME> + model. — как в README."""
    provider = {
        "name": PROVIDER_NAME,
        "base_url": base_url,
        "model": model,
        "discover_models": False,  # у прокси нет списка моделей
        "models": {model: {}},
        "context_length": context_length,
    }
    model_block = {
        "default": model,
        "provider": PROVIDER_NAME,
        "base_url": base_url,
    }
    return provider, model_block


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Подключить Hermes Agent к anonymizer-proxy (config.yaml)")
    parser.add_argument("--file", type=Path, default=None,
                        help="путь к config.yaml (по умолчанию — автопоиск)")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL,
                        help=f"адрес прокси (по умолчанию {DEFAULT_BASE_URL})")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help=f"имя модели (по умолчанию {DEFAULT_MODEL})")
    parser.add_argument("--context-length", type=int,
                        default=DEFAULT_CONTEXT_LENGTH,
                        help="размер контекста (прокси его не сообщает)")
    parser.add_argument("--dry-run", action="store_true",
                        help="показать изменения, файл не менять")
    parser.add_argument("--force", action="store_true",
                        help="не прерываться, если Hermes запущен")
    args = parser.parse_args()

    target = args.file or find_config()
    if target is None:
        print("[ОШИБКА] config.yaml Hermes не найден. Проверьте:", file=sys.stderr)
        for cand in config_candidates():
            print(f"  {cand}", file=sys.stderr)
        print("Hermes установлен? Если да — задайте путь через --file.",
              file=sys.stderr)
        return 1
    print(f"Конфиг Hermes: {target}")

    running = hermes_running()
    if running and not args.dry_run and not args.force:
        print("[ОШИБКА] Похоже, Hermes запущен "
              f"({', '.join(running)}).", file=sys.stderr)
        print("Закройте его: Hermes может кэшировать конфиг и перезаписать",
              file=sys.stderr)
        print("правку своим состоянием. Обход проверки: --force",
              file=sys.stderr)
        return 2

    try:
        with open(target, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except OSError as exc:
        print(f"[ОШИБКА] Не удалось прочитать {target}: {exc}", file=sys.stderr)
        return 1
    except yaml.YAMLError as exc:
        print(f"[ОШИБКА] {target} — некорректный YAML: {exc}", file=sys.stderr)
        return 1
    if not isinstance(data, dict):
        print(f"[ОШИБКА] {target}: неожиданная структура (не словарь)",
              file=sys.stderr)
        return 1

    fallback = data.get("fallback_model")
    if isinstance(fallback, dict) and fallback:
        print("[ВНИМАНИЕ] В конфиге активен fallback_model (облачный фоллбэк).",
              file=sys.stderr)
        print("По README это небезопасно: при сбое прокси запросы молча уйдут",
              file=sys.stderr)
        print("в облако в обход анонимизации. Рекомендуется отключить.",
              file=sys.stderr)

    provider, model_block = build_provider_block(
        args.base_url, args.model, args.context_length)
    providers = data.setdefault("providers", {})
    existed = PROVIDER_NAME in providers
    providers[PROVIDER_NAME] = provider
    data["model"] = model_block

    if args.dry_run:
        print("[DRY-RUN] Изменения НЕ записаны:")
        print(yaml.safe_dump(
            {"model": model_block, "providers": {PROVIDER_NAME: provider}},
            allow_unicode=True, sort_keys=False))
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = target.with_name(f"{target.name}.bak-{stamp}")
    shutil.copy2(target, backup)
    print(f"Резервная копия: {backup}")

    atomic_write(target, data)

    print()
    print("[OK] Готово. Провайдер «%s» %s в %s" % (
        PROVIDER_NAME, "обновлён" if existed else "добавлен", target))
    print(f"Модель по умолчанию: {args.model} (маршрутизируется на прокси)")
    print()
    print("Дальше: перезапустите Hermes — модель по умолчанию пойдёт через")
    print("anonymizer-proxy. Правила анонимизации ставятся отдельно:")
    print("  scripts/install_rules.py <путь-к-рабочей-папке>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
