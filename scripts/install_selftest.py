"""Самопроверка установки Anonymizer Proxy.

Проверяет три уровня:
1. Импорты ключевых пакетов (fastapi, uvicorn, httpx, gliner, natasha,
   onnxruntime, docx, openpyxl, pydantic, aiosqlite, dotenv).
2. Версию и доступные провайдеры onnxruntime.
3. Реальную загрузку обоих NER-движков (GLiNER ONNX + Natasha/Slovnet)
   и пробный прогон GLiNER на тестовой фразе.

В конце — сводка PASS/FAIL и код возврата 0/1 (для установщика).

Запуск:
    .venv\\Scripts\\python.exe scripts\\install_selftest.py   # Windows
    .venv/bin/python scripts/install_selftest.py             # macOS/Linux
"""
from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CHECK_IMPORTS = [
    "fastapi",
    "uvicorn",
    "httpx",
    "gliner",
    "natasha",
    "navec",
    "onnxruntime",
    "docx",
    "openpyxl",
    "pydantic",
    "aiosqlite",
    "dotenv",
]


def check_imports() -> bool:
    print("==> Шаг 1/3: импорты ключевых пакетов")
    ok = True
    for name in CHECK_IMPORTS:
        try:
            importlib.import_module(name)
            print(f"    OK  {name}")
        except ImportError as exc:
            ok = False
            print(f"    FAIL {name}: {exc}")
    return ok


def check_onnxruntime() -> bool:
    print("==> Шаг 2/3: onnxruntime")
    try:
        import onnxruntime as ort
    except ImportError as exc:
        print(f"    FAIL: {exc}")
        return False
    providers = ort.get_available_providers()
    print(f"    версия: {ort.__version__}")
    print(f"    провайдеры: {', '.join(providers)}")
    return True


def check_ner_engines() -> bool:
    print("==> Шаг 3/3: загрузка NER-движков и пробный прогон")
    from anonymizer_proxy.anonymizer.gliner_engine import GlinerEngine
    from anonymizer_proxy.anonymizer.natasha_engine import NatashaEngine
    from anonymizer_proxy.config import ensure_directories

    ensure_directories()
    ok = True

    gliner = GlinerEngine()
    try:
        started = time.monotonic()
        gliner._ensure_loaded()  # noqa: SLF001
        backend = gliner._backend or "?"  # noqa: SLF001
        providers = ", ".join(gliner._providers) or "нет"  # noqa: SLF001
        print(
            f"    GLiNER: бэкенд={backend}, провайдеры=[{providers}] "
            f"({time.monotonic() - started:.1f} с)"
        )
        # Синхронный прогон (категории-ключи, не метки модели)
        entities = gliner._predict(  # noqa: SLF001
            "Иван Петров работает в ООО Ромашка", ["PERSON", "ORG"]
        )
        print(f"    GLiNER пробный прогон: найдено сущностей: {len(entities)}")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"    GLiNER FAIL: {exc}")

    try:
        natasha = NatashaEngine()
        natasha._ensure_loaded()  # noqa: SLF001
        print("    Natasha/Slovnet: загружены")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"    Natasha FAIL: {exc}")
        print("    (контур можно отключить: NER_NATASHA=0 в .env)")

    return ok


def main() -> int:
    print("=== Самопроверка установки Anonymizer Proxy ===")
    results = [check_imports(), check_onnxruntime(), check_ner_engines()]
    print()
    if all(results):
        print("ИТОГ: PASS — установка готова к работе.")
        return 0
    print("ИТОГ: FAIL — см. сообщения выше.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
