#!/usr/bin/env python
"""
Скачивание GGUF-модели локальной LLM (llama-server) с HuggingFace
в ОБЩИЙ llama-рантайм машины (тот же каталог, что и у hermes-disk-search).

Запуск (из корня проекта):
    python scripts/download_llm_model.py                 # модель по умолчанию
    python scripts/download_llm_model.py <repo> <file>   # своя модель
Опции:
    --url URL        скачать по прямому URL (в обход repo/file)
    --out DIR        каталог назначения (по умолчанию models/chat общего
                     llama-рантайма: %LLAMA_RUNTIME_DIR% /
                     %LOCALAPPDATA%\\llama-runtime)
    --no-switch      не делать скачанную модель активной (манифест
                     current.json не трогать)
    --force          перекачать, даже если файл уже есть

Идемпотентно: существующий файл не перекачивается (без --force). После
загрузки в каталог чат-роли обновляется манифест models/chat/current.json
(общая активная модель для всех проектов; см. llama_runtime.py).
"""
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from anonymizer_proxy import llama_runtime  # noqa: E402  (путь — выше)

# Модель по умолчанию: qwen3.5-9b Q6_K (~7.5 ГБ) — общая чат-модель
# llama-рантайма (та же, что ставит ensure_llama_runtime.ps1;
# см. CHAT_PRESETS/DEFAULT_CHAT в llama_runtime.py).
DEFAULT_REPO = "unsloth/Qwen3.5-9B-GGUF"
DEFAULT_FILE = "Qwen3.5-9B-Q6_K.gguf"
OUT_DIR = llama_runtime.models_dir("chat")


def _human(n: float) -> str:
    for unit in ("Б", "КиБ", "МиБ", "ГиБ"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ТиБ"


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "anonymizer-proxy"})
    token = os.getenv("HF_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    print(f"Загрузка: {url}")
    print(f"Файл:     {dest}")
    with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as f:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total:
                pct = 100.0 * done / total
                print(f"\r  {pct:5.1f}%  {_human(done)} / {_human(total)}",
                      end="", flush=True)
    print()
    tmp.replace(dest)
    print(f"Готово: {dest} ({_human(dest.stat().st_size)})")


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    force = "--force" in args
    if force:
        args.remove("--force")
    switch = "--no-switch" not in args
    if not switch:
        args.remove("--no-switch")
    out_dir = OUT_DIR
    if "--out" in args:
        i = args.index("--out")
        out_dir = Path(args[i + 1])
        del args[i:i + 2]
    url = None
    if "--url" in args:
        i = args.index("--url")
        url = args[i + 1]
        del args[i:i + 2]

    if url is None:
        repo = args[0] if len(args) > 0 else DEFAULT_REPO
        filename = args[1] if len(args) > 1 else DEFAULT_FILE
        url = f"https://huggingface.co/{repo}/resolve/main/{filename}"
        filename = filename.split("/")[-1]
    else:
        filename = url.split("/")[-1].split("?")[0]

    dest = out_dir / filename
    if dest.is_file() and not force:
        print(f"Уже скачано: {dest} ({_human(dest.stat().st_size)}) — "
              "пропускаю (--force для перекачки)")
        _activate(dest, switch)
        return 0
    try:
        download(url, dest)
    except Exception as exc:  # noqa: BLE001 — ошибка сети не должна молчать
        print(f"[ОШИБКА] Не удалось скачать модель: {exc}", file=sys.stderr)
        if "401" in str(exc) or "403" in str(exc):
            print("Гейтнутый репозиторий? Задайте HF_TOKEN в переменных "
                  "окружения.", file=sys.stderr)
        dest.with_suffix(dest.suffix + ".part").unlink(missing_ok=True)
        return 1
    _activate(dest, switch)
    return 0


def _activate(dest: Path, switch: bool) -> None:
    """Сделать скачанный файл общей активной чат-моделью (манифест)."""
    if not switch or dest.parent != llama_runtime.models_dir("chat"):
        return
    try:
        llama_runtime.set_current_chat(dest.name)
        print(f"Активная чат-модель (манифест): {dest.name}")
        print("Примените к запущенным llama-серверам: "
              "python -m anonymizer_proxy.llama_runtime switch "
              f"{dest.name} (--no-download)")
    except Exception as exc:  # noqa: BLE001
        print(f"[ВНИМАНИЕ] Манифест не обновлён: {exc}")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())