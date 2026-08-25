"""Прогрев NER-моделей перед первым запуском прокси.

Скачивает и загружает оба NER-контура, чтобы установка работала офлайн:
- GLiNER ONNX (knowledgator/gliner-pii-large-v1.0) — тянется с HuggingFace;
- Natasha/Slovnet веса (~29 МБ) — с storage.yandexcloud.net в data/models/.

После загрузки печатает фактический бэкенд инференса и провайдеров — это же
значение показывает /health прокси. Повторный запуск ничего не перекачивает.

Запуск (после установки окружения):
    .venv\\Scripts\\python.exe scripts\\download_models.py     # Windows
    .venv/bin/python scripts/download_models.py               # macOS/Linux
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from anonymizer_proxy.anonymizer.gliner_engine import GlinerEngine  # noqa: E402
from anonymizer_proxy.anonymizer.natasha_engine import NatashaEngine  # noqa: E402
from anonymizer_proxy.config import ensure_directories  # noqa: E402


def main() -> int:
    ensure_directories()
    ok = True

    print("==> GLiNER ONNX: загрузка модели (при первом запуске — скачивание)…")
    started = time.monotonic()
    try:
        gliner = GlinerEngine()
        gliner._ensure_loaded()  # noqa: SLF001 — скрипту установки можно
        providers = ", ".join(gliner._providers) or "нет"  # noqa: SLF001
        backend = gliner._backend or "?"  # noqa: SLF001
        print(
            f"    OK за {time.monotonic() - started:.1f} с: "
            f"бэкенд={backend}, провайдеры=[{providers}]"
        )
    except Exception as exc:  # noqa: BLE001 — отчёт установщику нужен целиком
        ok = False
        print(f"    ПРОВАЛ: {exc}")

    print("==> Natasha/Slovnet: загрузка весов второго контура…")
    started = time.monotonic()
    try:
        natasha = NatashaEngine()
        natasha._ensure_loaded()  # noqa: SLF001
        print(f"    OK за {time.monotonic() - started:.1f} с")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"    ПРОВАЛ: {exc}")
        print("    (контур можно отключить: NER_NATASHA=0 в .env)")

    if not ok:
        print("\nИТОГ: есть ошибки — см. сообщения выше.", file=sys.stderr)
        return 1
    print("\nИТОГ: модели готовы, первый запуск прокси пройдёт без скачивания.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
