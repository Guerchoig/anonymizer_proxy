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
    print("==> Шаг 1/5: импорты ключевых пакетов")
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
    print("==> Шаг 2/5: onnxruntime")
    try:
        import onnxruntime as ort
    except ImportError as exc:
        print(f"    FAIL: {exc}")
        return False
    providers = ort.get_available_providers()
    print(f"    версия: {ort.__version__}")
    print(f"    провайдеры: {', '.join(providers)}")
    return True


# Предупреждения, не фейлящие установку (заполняется check_provider_key)
WARNINGS: list[str] = []


def check_provider_key() -> bool:
    """Шаг 4/5: конфигурация ДЕЙСТВУЮЩЕГО облачного провайдера.

    Отсутствие/невалидность ключа НЕ фейлит установку (локальная анонимизация
    самодостаточна), но печатается заметное предупреждение с инструкцией.
    Сразу после установки действующий провайдер — стартовый CLOUD_PROVIDER
    (openrouter); после явного переключения в runtime_state.json.
    """
    print("==> Шаг 4/5: облачный провайдер")
    import os

    import httpx

    from anonymizer_proxy.config import CLOUD_PROVIDERS, acting_cloud_provider

    name = acting_cloud_provider()
    cfg = CLOUD_PROVIDERS.get(name) or {}
    key_env = ("OPENROUTER_API_KEY" if name == "openrouter"
               else f"{name.upper()}_API_KEY")
    keys_url = cfg.get("keys_url") or "личный кабинет провайдера"

    print(f"    Действующий облачный провайдер: {name} ({cfg.get('base_url')})")
    key = cfg.get("api_key") or ""
    if not key or "REPLACE_WITH" in key.upper():
        WARNINGS.append(f"{key_env} не задан")
        print(f"    [ВНИМАНИЕ] {key_env} не задан (или остался плейсхолдером):")
        print("    анонимизация работать будет, а вот запросы к облаку упадут с 401.")
        print(f"    Вставьте ключ с {keys_url} в .env (или через форму")
        print("    http://127.0.0.1:8081/env-editor) и перезапустите прокси.")
        return True

    if name == "openrouter":
        # Живая проверка ключа через OpenRouter (учитывая VPN-прокси)
        proxy = os.getenv("OPENROUTER_PROXY") or None
        try:
            response = httpx.get(
                "https://openrouter.ai/api/v1/auth/key",
                headers={"Authorization": f"Bearer {key}"},
                timeout=15,
                proxy=proxy,
            )
        except Exception as exc:  # noqa: BLE001 — сеть может быть недоступна
            WARNINGS.append("ключ не удалось проверить онлайн")
            print(f"    [ВНИМАНИЕ] Не удалось проверить ключ онлайн ({exc}).")
            print("    Если OpenRouter требует VPN — проверьте OPENROUTER_PROXY в .env.")
            return True

        if response.status_code == 200:
            label = response.json().get("data", {}).get("label") or "(без имени)"
            print(f"    OK: ключ действителен ({label})")
            return True

        WARNINGS.append(f"OpenRouter ответил {response.status_code}")
        print(f"    [ВНИМАНИЕ] OpenRouter ответил {response.status_code}: {response.text[:120]}")
        print("    Проверьте OPENROUTER_API_KEY в .env.")
        return True

    # Остальные провайдеры: живая проверка через их GET /models
    # (у российских провайдеров — прямой доступ, без VPN)
    try:
        response = httpx.get(
            f"{cfg.get('base_url')}/models",
            headers={"Authorization": f"Bearer {key}"},
            timeout=15,
        )
    except Exception as exc:  # noqa: BLE001
        WARNINGS.append("ключ не удалось проверить онлайн")
        print(f"    [ВНИМАНИЕ] Не удалось проверить ключ онлайн ({exc}).")
        return True

    if response.status_code == 200:
        try:
            n = len(response.json().get("data") or [])
            print(f"    OK: ключ принят, доступно моделей: {n}")
        except Exception:  # noqa: BLE001
            print("    OK: ключ принят (список моделей не разобран)")
        return True

    if response.status_code in (401, 403):
        WARNINGS.append(f"{name} отверг ключ (HTTP {response.status_code})")
        print(f"    [ВНИМАНИЕ] {name} отверг ключ (HTTP {response.status_code}).")
        print(f"    Проверьте {key_env} в .env.")
        return True

    WARNINGS.append(f"{name} ответил {response.status_code} на /models")
    print(f"    [ВНИМАНИЕ] {name} ответил {response.status_code} на /models —")
    print("    ключ задан, но живую проверку выполнить не удалось (не критично).")
    return True


def check_ner_engines() -> bool:
    print("==> Шаг 3/5: загрузка NER-движков и пробный прогон")
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


def check_llm_server() -> bool:
    """Шаг 5/5: llama-server (локальная LLM).

    НЕ фейлит установку: облачные бэкенды работают без llama-server.
    Проверяется: бинарь найден (LLM_SERVER_BIN / PATH / tools/llama.cpp /
    Homebrew), GGUF-модель существует, порт свободен или занят живым
    llama-инстансом.
    """
    print("==> Шаг 5/5: llama-server (локальная LLM)")
    from anonymizer_proxy import llm_server, llama_runtime
    from anonymizer_proxy.config import LLM_SERVER

    bin_path = llm_server.find_binary()
    if not bin_path:
        WARNINGS.append("llama-server не найден (LLM_SERVER_BIN пуст)")
        print("    [ВНИМАНИЕ] llama-server не найден (общий llama-рантайм / "
              "PATH / tools/llama.cpp / Homebrew).")
        print("    Локальный LLM-бэкенд не поднимется; установите общий "
              f"рантайм: {llama_runtime.install_hint()}")
        print("    (или укажите LLM_SERVER_BIN в .env). Облачные бэкенды "
              "работают как обычно.")
        return True
    print(f"    OK: llama-server найден: {bin_path}")

    # Модель: shared:chat — файл из манифеста общего llama-рантайма
    spec = LLM_SERVER["model"] or "shared:chat"
    try:
        model = llama_runtime.resolve_model(spec, role="chat")
    except FileNotFoundError as exc:
        model = None
        print(f"    [ВНИМАНИЕ] {exc}")
    if model is None or not model.is_file():
        WARNINGS.append("GGUF-модель не найдена")
        py = (r".venv\Scripts\python.exe" if sys.platform == "win32"
              else ".venv/bin/python")
        print(f"    [ВНИМАНИЕ] GGUF-модель не найдена: {spec}")
        print("    Скачайте общую чат-модель: кнопка «Применить» на странице "
              "/env-editor либо")
        print(f"    {py} -m anonymizer_proxy.llama_runtime "
              "switch Qwen3.5-9B-Q6_K.gguf")
        return True
    print(f"    OK: GGUF-модель: {model}")

    det = llm_server.probe()
    if det["state"] == llm_server.STATE_LLAMA:
        print(f"    OK: llama-server уже запущен "
              f"({det['total_slots']} слот(ов))")
    elif det["state"] == llm_server.STATE_FOREIGN:
        WARNINGS.append(f"порт {LLM_SERVER['port']} занят посторонним сервисом")
        print(f"    [ВНИМАНИЕ] Порт {LLM_SERVER['port']} занят посторонним "
              "сервисом — смените LLM_SERVER_PORT в .env.")
    else:
        print(f"    OK: порт {LLM_SERVER['port']} свободен — прокси запустит "
              "llama-server при старте (LLM_SERVER_AUTOSTART=1)")
    return True


def main() -> int:
    print("=== Самопроверка установки Anonymizer Proxy ===")
    results = [
        check_imports(),
        check_onnxruntime(),
        check_ner_engines(),
        check_provider_key(),
        check_llm_server(),
    ]
    print()
    if not all(results):
        print("ИТОГ: FAIL — см. сообщения выше.", file=sys.stderr)
        return 1
    if WARNINGS:
        print("ИТОГ: PASS, но есть предупреждения:")
        for warning in WARNINGS:
            print(f"  - {warning}")
        return 0
    print("ИТОГ: PASS — установка готова к работе.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
