"""
Тесты мультипровайдерного облачного слоя (CLOUD_PROVIDERS) и семантики
ДЕЙСТВУЮЩЕГО облачного провайдера.

Проверяют:
1. Реестр провайдеров (без Chad): имена, base URL, у российских провайдеров
   proxy=None (прямой доступ БЕЗ VPN, даже если OPENROUTER_PROXY задан),
   у OpenRouter — прокси из .env; браузерный UA и служебные заголовки —
   только OpenRouter.
2. Схемы авторизации: GPTunneL — ключ в Authorization БЕЗ Bearer (plain),
   остальные — Bearer.
3. Действующий облачный провайдер (RUNTIME["cloud"]): явное переключение
   запоминает провайдера, переход на local память сохраняет, «работай через
   облако» и cloud/… возвращают к действующему; миграция старого
   runtime_state.json без поля "cloud".
4. Команды переключения на конкретных провайдеров УДАЛЕНЫ: фразы
   «переключись на ботхаб» и т.п. больше не команды (и не проходят
   префильтр) — снижена вероятность ложной детекции.
5. Делегирование: запрос с backend="<провайдер>" уходит в клиента этого
   провайдера (с моделью из префикса, если она была).

Запуск: python anonymizer_proxy\\tests\\test_cloud_providers.py (из корня)
"""
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from anonymizer_proxy.config import (
    CLOUD_PROVIDER, CLOUD_PROVIDERS, RUNTIME, RUNTIME_STATE_PATH,
    acting_cloud_provider, load_runtime_state,
)
from anonymizer_proxy.proxy.llm_router import LLMRouter
from anonymizer_proxy.proxy.openrouter_client import OpenAICompatClient
from anonymizer_proxy.models.schemas import ChatCompletionRequest, ChatMessage
from anonymizer_proxy.proxy import handlers as handlers_module
from anonymizer_proxy.proxy.handlers import RequestHandler
from anonymizer_proxy.proxy.command_classifier import looks_like_command
from anonymizer_proxy.tests.test_tools_manual import FakeNER, FakeStore

handlers_module.CURRENT_MODE = "manual"


def test_registry_sanity():
    """Реестр (без Chad): имена, base URL, изоляция VPN-прокси"""
    assert CLOUD_PROVIDER in CLOUD_PROVIDERS
    assert "chad" not in CLOUD_PROVIDERS, "Chad должен быть удалён из реестра"
    expected_urls = {
        "openrouter": "openrouter.ai/api/v1",
        "gptunnel": "gptunnel.ru/v1",
        "bothub": "bothub.chat/api/v2/openai/v1",
        "aitunnel": "api.aitunnel.ru/v1",
        "genapi": "proxy.gen-api.ru/v1",
    }
    for name, url in expected_urls.items():
        assert name in CLOUD_PROVIDERS, f"нет провайдера {name}"
        assert url in CLOUD_PROVIDERS[name]["base_url"], \
            f"{name}: неожиданный base_url {CLOUD_PROVIDERS[name]['base_url']}"

    env_proxy = os.getenv("OPENROUTER_PROXY", "")
    for name, cfg in CLOUD_PROVIDERS.items():
        if name == "openrouter":
            assert (cfg["proxy"] or "") == env_proxy, \
                "OpenRouter должен использовать OPENROUTER_PROXY (VPN)"
        else:
            assert not cfg["proxy"], \
                f"{name} должен ходить БЕЗ VPN (proxy=None), а не {cfg['proxy']}"
    print("TEST 1 OK: реестр провайдеров (без Chad), VPN только у OpenRouter")


def test_auth_schemes_and_headers():
    """Авторизация: plain у GPTunneL, Bearer у остальных; UA/заголовки —
    только OpenRouter"""
    openrouter = OpenAICompatClient(CLOUD_PROVIDERS["openrouter"])
    assert openrouter.auth_scheme == "bearer"
    assert openrouter.browser_ua
    headers = openrouter._auth_headers()
    assert headers["Authorization"] == f"Bearer {openrouter.api_key}"
    assert "User-Agent" in headers and "HTTP-Referer" in headers

    gptunnel = OpenAICompatClient(CLOUD_PROVIDERS["gptunnel"])
    assert gptunnel.auth_scheme == "plain"
    assert not gptunnel.browser_ua
    headers = gptunnel._auth_headers()
    assert headers["Authorization"] == gptunnel.api_key, \
        "GPTunneL: ключ в Authorization БЕЗ префикса Bearer"
    assert "HTTP-Referer" not in headers and "User-Agent" not in headers

    for name in ("bothub", "aitunnel", "genapi"):
        client = OpenAICompatClient(CLOUD_PROVIDERS[name])
        assert client.auth_scheme == "bearer", name
        assert not client.browser_ua, name
        assert client._auth_headers()["Authorization"].startswith("Bearer "), name
        assert client.proxy is None, f"{name} не должен иметь VPN-прокси"
    print("TEST 2 OK: схемы авторизации и заголовки провайдеров")


class FakeCapture:
    """Минимальный фейк облачного клиента (без стрима)"""

    def __init__(self):
        self.calls = []

    async def chat_completion(self, messages, model=None, **kwargs):
        self.calls.append({"messages": messages, "model": model, **kwargs})
        return {}

    async def chat_completion_stream(self, messages, model=None, **kwargs):
        self.calls.append({"messages": messages, "model": model, **kwargs})
        return
        yield  # pragma: no cover — чтобы функция была асинхронным генератором


def make_router() -> LLMRouter:
    return LLMRouter()


def test_router_resolution():
    """Роутинг: префикс провайдера, cloud/ -> ДЕЙСТВУЮЩИЙ, явный backend"""
    router = make_router()
    old_backend = RUNTIME["backend"]
    old_cloud = RUNTIME["cloud"]
    try:
        RUNTIME["backend"] = "openrouter"
        RUNTIME["cloud"] = "openrouter"
        assert router._resolve("aitunnel/deepseek-v4-pro") == "aitunnel"
        assert router._resolve("Bothub/gpt-4o") == "bothub"
        assert router._resolve("qwen/qwen-3.7-max") == "openrouter", \
            "обычные префиксы моделей OpenRouter — это не провайдеры"
        assert router._resolve("cloud/anything") == "openrouter"
        assert router._resolve(None, backend="gptunnel") == "gptunnel"
        assert router._resolve(None, backend="local") == "local"
        try:
            router._resolve(None, backend="chad")
            assert False, "chad удалён — должен быть ValueError"
        except ValueError:
            pass
        # Действующий провайдер: явное переключение запоминается, local
        # память не стирает, cloud/… возвращается к нему
        router.set_backend("bothub")
        assert acting_cloud_provider() == "bothub"
        router.set_backend("local")
        assert RUNTIME["backend"] == "local"
        assert acting_cloud_provider() == "bothub", \
            "переход на local не должен забывать действующего провайдера"
        assert router._resolve("cloud/anything") == "bothub"
        assert router._resolve(None) == "local"
    finally:
        RUNTIME["backend"] = old_backend
        RUNTIME["cloud"] = old_cloud
    print("TEST 3 OK: роутинг (префикс провайдера, cloud/ -> действующий)")


def test_set_backend_persists():
    """set_backend: провайдер становится действующим, поля backend+cloud
    персистятся в runtime_state.json"""
    router = make_router()
    old_backend = RUNTIME["backend"]
    old_cloud = RUNTIME["cloud"]
    try:
        router.set_backend("bothub")
        assert RUNTIME["backend"] == "bothub"
        assert RUNTIME["cloud"] == "bothub"
        state = json.loads(RUNTIME_STATE_PATH.read_text(encoding="utf-8"))
        assert state["backend"] == "bothub" and state["cloud"] == "bothub", state
        router.set_backend("local")
        state = json.loads(RUNTIME_STATE_PATH.read_text(encoding="utf-8"))
        assert state["backend"] == "local" and state["cloud"] == "bothub", \
            "переход на local не должен затирать действующего провайдера"
        try:
            router.set_backend("yandex")
            assert False, "должен быть ValueError"
        except ValueError:
            pass
    finally:
        RUNTIME["backend"] = old_backend
        RUNTIME["cloud"] = old_cloud
        router.set_backend(old_backend)
    print("TEST 4 OK: set_backend — действующий провайдер + персист обоих полей")


def test_legacy_runtime_state_migration():
    """Старый runtime_state.json без поля "cloud" мигрирует: облачный
    backend -> cloud = backend; local -> cloud = стартовый CLOUD_PROVIDER"""
    backup = None
    if RUNTIME_STATE_PATH.is_file():
        backup = RUNTIME_STATE_PATH.read_text(encoding="utf-8")
    old_backend = RUNTIME["backend"]
    old_cloud = RUNTIME["cloud"]
    try:
        RUNTIME_STATE_PATH.write_text(
            json.dumps({"backend": "bothub"}), encoding="utf-8")
        load_runtime_state()
        assert RUNTIME["backend"] == "bothub"
        assert RUNTIME["cloud"] == "bothub", \
            "миграция: облачный backend становится действующим"

        RUNTIME_STATE_PATH.write_text(
            json.dumps({"backend": "local"}), encoding="utf-8")
        load_runtime_state()
        assert RUNTIME["backend"] == "local"
        assert RUNTIME["cloud"] == CLOUD_PROVIDER, \
            "миграция: local не даёт информации о провайдере — берём стартовый"
    finally:
        if backup is not None:
            RUNTIME_STATE_PATH.write_text(backup, encoding="utf-8")
        else:
            RUNTIME_STATE_PATH.unlink(missing_ok=True)
        RUNTIME["backend"] = old_backend
        RUNTIME["cloud"] = old_cloud
    print("TEST 5 OK: миграция старого runtime_state.json")


def test_delegation_to_provider_client():
    """Запрос с backend=<провайдер> уходит в клиента этого провайдера;
    модель из префикса '<провайдер>/<модель>' передаётся клиенту"""
    router = make_router()
    fake_default = FakeCapture()
    fake_bothub = FakeCapture()
    fake_aitunnel = FakeCapture()
    router._openrouter = fake_default
    router._cloud["bothub"] = fake_bothub
    router._cloud["aitunnel"] = fake_aitunnel
    old_backend = RUNTIME["backend"]
    old_cloud = RUNTIME["cloud"]
    try:
        RUNTIME["backend"] = "openrouter"
        RUNTIME["cloud"] = "openrouter"
        asyncio.run(router.chat_completion(
            messages=[{"role": "user", "content": "x"}],
            model="openrouter/qwen-3.7-max", backend="openrouter"))
        # OpenRouter-клиенту модель НЕ передаётся — он берёт её из .env
        assert fake_default.calls[0]["model"] is None

        asyncio.run(router.chat_completion(
            messages=[{"role": "user", "content": "x"}],
            model="bothub/gpt-4o", backend="bothub"))
        assert fake_bothub.calls[0]["model"] == "gpt-4o"

        asyncio.run(router.chat_completion(
            messages=[{"role": "user", "content": "x"}],
            model=None, backend="aitunnel"))
        assert fake_aitunnel.calls[0]["model"] is None
    finally:
        RUNTIME["backend"] = old_backend
        RUNTIME["cloud"] = old_cloud
    print("TEST 6 OK: делегирование в клиентов конкретных провайдеров")


def make_request(text: str) -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model="m", anonymize=True, stream=False,
        messages=[ChatMessage(role="user", content=text)],
    )


def test_chat_command_cloud_only():
    """Осталась только общая команда «работай через облако» (-> действующий
    провайдер); именованных команд переключения больше НЕТ"""
    handler = RequestHandler(
        ner_service=FakeNER({}),
        mapping_store=FakeStore(),
        openrouter_client=make_router(),
    )
    old_backend = RUNTIME["backend"]
    old_cloud = RUNTIME["cloud"]
    try:
        RUNTIME["backend"] = "local"
        RUNTIME["cloud"] = "bothub"
        # «работай через облако» — возврат к ДЕЙСТВУЮЩЕМУ провайдеру
        assert handler.detect_backend_switch(
            make_request("работай через облако")) == "bothub"
        assert handler.detect_backend_switch(
            make_request("переключись на облачную модель")) == "bothub"
        assert handler.detect_backend_switch(
            make_request("вернись в облако")) == "bothub"
        # именованные команды удалены — фразы с именами провайдеров больше
        # НЕ команды (уходят в модель как обычный текст)
        for phrase in ("переключись на ботхаб", "работай через айтуннел",
                       "используй генапи", "верни опенроутер"):
            assert handler.detect_backend_switch(
                make_request(phrase)) is None, phrase
            # и префильтр их больше не пропускает как кандидатов команд
            assert not looks_like_command([phrase]), \
                f"префильтр всё ещё ловит: {phrase}"
        # «…бэкенд…» проходит префильтр (кандидат), но конкретного
        # провайдера больше не распознаёт — детекции нет
        phrase = "переключи бэкенд на гптуннел"
        assert handler.detect_backend_switch(make_request(phrase)) is None
        # «локальная модель» работает как раньше
        assert handler.detect_backend_switch(
            make_request("работай через локальную модель")) == "local"
        # перезапуск приоритетнее переключения
        assert handler.detect_backend_switch(
            make_request("перезапусти прокси и работай в облаке")) is None
        # свежая установка (без памяти о переключениях) — openrouter
        RUNTIME["cloud"] = CLOUD_PROVIDER
        assert handler.detect_backend_switch(
            make_request("работай через облако")) == CLOUD_PROVIDER
    finally:
        RUNTIME["backend"] = old_backend
        RUNTIME["cloud"] = old_cloud
    print("TEST 7 OK: команды — только «облако»/«локальная»; именованных нет")


def main():
    test_registry_sanity()
    test_auth_schemes_and_headers()
    test_router_resolution()
    test_set_backend_persists()
    test_legacy_runtime_state_migration()
    test_delegation_to_provider_client()
    test_chat_command_cloud_only()
    print("\nALL CLOUD PROVIDERS TESTS PASSED")


if __name__ == "__main__":
    main()
