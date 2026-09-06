"""
Тесты точечных чат-команд прокси (v1.11):
- «дополнительно анонимизируй: …» — доп. анонимизация перечисленных имён;
- «деанонимизируй плейсхолдеры …» — выборочная деанонимизация списка/диапазона.

Детекция — только детерминированные правила (без LLM); парсеры слотов —
чистые функции. Проверяются конфликты формулировок (плейсхолдерная команда
НЕ должна перехватываться полной деанонимизацией и наоборот, анти-fallthrough)
и гарантия статуса: файл результата по-прежнему считается анонимизированным.

Запуск: python anonymizer_proxy\\tests\\test_extra_commands.py (из корня)
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from anonymizer_proxy.models.schemas import ChatCompletionRequest
from anonymizer_proxy.proxy import handlers as handlers_module
from anonymizer_proxy.proxy.command_args import (
    parse_name_list,
    parse_placeholder_spec,
)
from anonymizer_proxy.proxy.handlers import RequestHandler
from anonymizer_proxy.tests.test_tools_passthrough import (
    FakeNER,
    FakeOpenRouter,
    FakeStore,
)

handlers_module.CURRENT_MODE = "passthrough"


class FakeStoreWithLookup(FakeStore):
    """FakeStore + get_latest_session_for_file (как в MappingStore)."""

    async def get_latest_session_for_file(self, file_path):
        return self.file_sessions.get(str(file_path))


def _make_handler(store=None) -> RequestHandler:
    return RequestHandler(
        ner_service=FakeNER({}),
        mapping_store=store or FakeStoreWithLookup(),
        openrouter_client=FakeOpenRouter(),
    )


def _request(*messages) -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model="m", messages=list(messages), stream=False)


def test_parse_placeholder_spec_lists_and_ranges():
    tokens, errors = parse_placeholder_spec(
        "деанонимизируй плейсхолдеры PERSON_1, [PERSON_3]-PERSON_5 и ORG_2")
    assert tokens == ["[PERSON_1]", "[PERSON_3]", "[PERSON_4]",
                      "[PERSON_5]", "[ORG_2]"], tokens
    assert errors == []
    print("TEST 1 OK: parse_placeholder_spec — списки и диапазоны")


def test_parse_placeholder_spec_range_forms():
    for phrase in ("PERSON_3–PERSON_5", "PERSON_3..PERSON_5",
                   "PERSON_3 - 5", "с PERSON_3 по PERSON_5"):
        tokens, errors = parse_placeholder_spec(phrase)
        assert tokens == ["[PERSON_3]", "[PERSON_4]", "[PERSON_5]"], phrase
        assert errors == []
    print("TEST 2 OK: parse_placeholder_spec — все формы диапазонов")


def test_parse_placeholder_spec_errors():
    tokens, errors = parse_placeholder_spec(
        "PERSON_3–ORG_5 и FOO_1 и PERSON_9–PERSON_2")
    assert tokens == [], tokens
    assert any("одного типа" in e for e in errors)
    assert any("FOO_1" in e for e in errors)
    assert any("меньше начала" in e for e in errors)
    print("TEST 3 OK: parse_placeholder_spec — ошибки не фатальны")


def test_parse_name_list():
    names, errors = parse_name_list(
        ": Иванов, Петрова и «Сидоров И. И.» в файле C:\\docs\\KP.docx")
    assert names == ["Иванов", "Петрова", "Сидоров И. И."], names
    assert errors == []
    print("TEST 4 OK: parse_name_list — запятые, «и», кавычки, инициалы")


def test_parse_name_list_garbage():
    names, errors = parse_name_list(": Иванов, 12345, @@!!")
    assert names == ["Иванов"], names
    assert len(errors) == 2, errors
    print("TEST 5 OK: parse_name_list — мусор в errors, имена в names")


def test_resolve_placeholder_deanon():
    with tempfile.TemporaryDirectory() as td:
        result = Path(td) / "doc.result.txt"
        copy = Path(td) / "doc.anonymized.txt"
        result.write_text("[PERSON_1] проверил.", encoding="utf-8")
        copy.write_text("[PERSON_1] подписал.", encoding="utf-8")
        history = ("[ANONYMIZER] Файлы анонимизированы.\n"
                   "session_id: sess-b1\n"
                   f"[anonymizer:result:{result}]\n"
                   f"[anonymizer:copy:{copy}]")
        handler = _make_handler()

        # Каноническая команда → deanon_placeholders (НЕ deanon_files)
        req = _request(
            {"role": "user", "content": "анонимизируй файл doc.docx"},
            {"role": "assistant", "content": history},
            {"role": "user", "content":
                "деанонимизируй плейсхолдеры PERSON_1, PERSON_3–PERSON_5"},
        )
        cmd = asyncio.run(handler.resolve_chat_command(req))
        assert cmd["command"] == "deanon_placeholders", cmd
        assert cmd["tokens"] == ["[PERSON_1]", "[PERSON_3]", "[PERSON_4]",
                                 "[PERSON_5]"]
        assert cmd["targets"][0]["session_id"] == "sess-b1"

        # Anti-fallthrough: интент есть, токенов нет — подсказка, а не полная
        # деанонимизация
        req_bad = _request(
            {"role": "assistant", "content": history},
            {"role": "user", "content": "деанонимизируй плейсхолдеры"},
        )
        cmd_bad = asyncio.run(handler.resolve_chat_command(req_bad))
        assert cmd_bad["command"] == "cmd_help", cmd_bad

        # Совместимость: «деанонимизируй файлы» — прежняя полная деанонимизация
        req_old = _request(
            {"role": "assistant", "content": history},
            {"role": "user", "content": "деанонимизируй файлы"},
        )
        cmd_old = asyncio.run(handler.resolve_chat_command(req_old))
        assert cmd_old["command"] == "deanon_files", cmd_old

        # Без слова «плейсхолдеры», но с токеном — частичная деанонимизация
        req_bare = _request(
            {"role": "assistant", "content": history},
            {"role": "user", "content": "деанонимизируй PERSON_2"},
        )
        cmd_bare = asyncio.run(handler.resolve_chat_command(req_bare))
        assert cmd_bare["command"] == "deanon_placeholders", cmd_bare
        assert cmd_bare["tokens"] == ["[PERSON_2]"]
    print("TEST 6 OK: resolve — приоритет над deanon_files, anti-fallthrough")


def test_resolve_extra_anonymize():
    with tempfile.TemporaryDirectory() as td:
        copy = Path(td) / "doc.anonymized.txt"
        copy.write_text("Иванов подписал.", encoding="utf-8")
        history = ("[ANONYMIZER] Файлы анонимизированы.\n"
                   "session_id: sess-a1\n"
                   f"[anonymizer:copy:{copy}]")
        handler = _make_handler()

        req = _request(
            {"role": "assistant", "content": history},
            {"role": "user", "content":
                "дополнительно анонимизируй: Иванов, Петрова"},
        )
        cmd = asyncio.run(handler.resolve_chat_command(req))
        assert cmd["command"] == "extra_anonymize", cmd
        assert cmd["names"] == ["Иванов", "Петрова"]
        assert cmd["targets"][0]["copy_path"] == str(copy)

        # Anti-fallthrough: без имён — подсказка, а не whole-file NER
        req_bad = _request(
            {"role": "assistant", "content": history},
            {"role": "user", "content": "дополнительно анонимизируй"},
        )
        cmd_bad = asyncio.run(handler.resolve_chat_command(req_bad))
        assert cmd_bad["command"] == "cmd_help", cmd_bad

        # Обычная команда анонимизации файла — не новая команда
        req_plain = _request(
            {"role": "user", "content": "анонимизируй файл doc.docx"})
        assert asyncio.run(
            handler.resolve_chat_command(req_plain)) is None
    print("TEST 7 OK: resolve extra_anonymize — имена, anti-fallthrough, "
          "не мешает штатной анонимизации")


def test_roundtrip_extra_anonymize():
    async def _run():
        with tempfile.TemporaryDirectory() as td:
            copy = Path(td) / "doc.anonymized.txt"
            copy.write_text("Иванов подписал, а Петров проверил.\n",
                            encoding="utf-8")
            store = FakeStoreWithLookup()
            handler = _make_handler(store)
            history = ("[ANONYMIZER] Файлы анонимизированы.\n"
                       "session_id: sess-a1\n"
                       f"[anonymizer:copy:{copy}]")
            req = _request(
                {"role": "assistant", "content": history},
                {"role": "user", "content":
                    "дополнительно анонимизируй: Иванов, Петров, Сидоров"},
            )
            cmd = await handler.resolve_chat_command(req)
            assert cmd["command"] == "extra_anonymize"
            response, kind = await handler.execute_chat_command(req, cmd)
            assert kind == "extra_anonymize"
            content = copy.read_text(encoding="utf-8")
            assert "Иванов" not in content and "Петров" not in content, content
            assert "[PERSON_" in content
            text = response.choices[0].message.content
            assert "Иванов → [PERSON_" in text
            assert "Сидоров" in text  # не найдено — честный отчёт
            assert "session_id: sess-a1" in text
            assert "[anonymizer:copy:" in text
            mappings = await store.get_all_mappings("sess-a1")
            assert set(mappings.values()) == {"Иванов", "Петров"}, mappings
    asyncio.run(_run())
    print("TEST 8 OK: roundtrip extra_anonymize — замены, отчёт, маппинги")


def test_roundtrip_placeholders_partial_deanon():
    async def _run():
        with tempfile.TemporaryDirectory() as td:
            copy = Path(td) / "doc.anonymized.txt"
            copy.write_text("[PERSON_1] подписал у [ORG_1].",
                            encoding="utf-8")
            result = Path(td) / "doc.result.txt"
            result.write_text("[PERSON_1] проверил [PERSON_2].",
                              encoding="utf-8")
            copy_bytes = copy.read_bytes()
            store = FakeStoreWithLookup()
            store.mappings = {"[PERSON_1]": "Иванов", "[PERSON_2]": "Петров",
                              "[ORG_1]": "ООО Ромашка"}
            handler = _make_handler(store)
            history = ("[ANONYMIZER] Файлы анонимизированы.\n"
                       "session_id: sess-b1\n"
                       f"[anonymizer:result:{result}]\n"
                       f"[anonymizer:copy:{copy}]")

            # Выборочная деанонимизация: PERSON_1 и несуществующие PERSON_3/4
            req = _request(
                {"role": "assistant", "content": history},
                {"role": "user", "content":
                    "деанонимизируй плейсхолдеры PERSON_1, PERSON_3–PERSON_4"},
            )
            cmd = await handler.resolve_chat_command(req)
            response, kind = await handler.execute_chat_command(req, cmd)
            assert kind == "placeholders_deanonymization"
            text = response.choices[0].message.content
            assert result.read_text(encoding="utf-8") == \
                "Иванов проверил [PERSON_2]."
            assert copy.read_bytes() == copy_bytes  # копия не тронута
            assert "по-прежнему считается анонимизированным" in text
            assert "PERSON_3" in text and "PERSON_4" in text
            assert set((await store.get_all_mappings("sess-b1")).keys()) == {
                "[PERSON_1]", "[PERSON_2]", "[ORG_1]"}

            # Файл всё ещё «в конвейере»: повторная выборочная деанон работает
            req2 = _request(
                {"role": "assistant", "content": history},
                {"role": "user", "content":
                    "деанонимизируй плейсхолдеры PERSON_2"},
            )
            cmd2 = await handler.resolve_chat_command(req2)
            await handler.execute_chat_command(req2, cmd2)
            assert result.read_text(encoding="utf-8") == \
                "Иванов проверил Петров."
            # Даже после восстановления ВСЕХ плейсхолдеров файла копия
            # по-прежнему не тронута — файл считается анонимизированным
            assert copy.read_bytes() == copy_bytes
    asyncio.run(_run())
    print("TEST 9 OK: roundtrip placeholders — выборочность, статус файла, "
          "повторные команды")


if __name__ == "__main__":
    test_parse_placeholder_spec_lists_and_ranges()
    test_parse_placeholder_spec_range_forms()
    test_parse_placeholder_spec_errors()
    test_parse_name_list()
    test_parse_name_list_garbage()
    test_resolve_placeholder_deanon()
    test_resolve_extra_anonymize()
    test_roundtrip_extra_anonymize()
    test_roundtrip_placeholders_partial_deanon()
    print("\nALL EXTRA COMMANDS TESTS PASSED")


