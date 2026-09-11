"""
Тесты точечных чат-команд прокси (v1.11):
- «скрой эти данные: …» — доп. анонимизация перечисленных имён;
- «раскрой эти данные …» — выборочная деанонимизация списка/диапазона.

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
from anonymizer_proxy.tests.test_tools_manual import (
    FakeNER,
    FakeOpenRouter,
    FakeStore,
)

handlers_module.CURRENT_MODE = "manual"


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
        "раскрой эти данные PERSON_1, [PERSON_3]-PERSON_5 и ORG_2")
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
    names, errors = parse_name_list(": Иванов, @@!!")
    assert names == ["Иванов"], names
    assert errors == ["@@!!"], errors
    print("TEST 5 OK: parse_name_list — мусор в errors, имена в names")


def test_parse_name_list_arbitrary_values():
    """Регрессия 2026-09-09: значения НЕ обязаны быть именами людей —
    номера договоров, даты, любые строки с буквой/цифрой."""
    names, errors = parse_name_list(
        ': "0095/23/2.1/00075271/013/2023", "27.12.2023"')
    assert names == ["0095/23/2.1/00075271/013/2023", "27.12.2023"], names
    assert errors == [], errors

    # без кавычек, с «и», с числами внутри фразы
    names2, errors2 = parse_name_list(
        ": 0095/23/2.1/00075271/013/2023 и 27.12.2023 и Договор №123 от 01.02.2024")
    assert names2 == ["0095/23/2.1/00075271/013/2023", "27.12.2023",
                      "Договор №123 от 01.02.2024"], names2
    assert errors2 == [], errors2

    # мусор по-прежнему отсеивается
    names3, errors3 = parse_name_list(": Иванов, !!!!")
    assert names3 == ["Иванов"], names3
    assert errors3 == ["!!!!"], errors3
    print("TEST 5b OK: parse_name_list — произвольные значения (номера, "
          "даты) принимаются")


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
            {"role": "user", "content": "скрой все данные doc.docx"},
            {"role": "assistant", "content": history},
            {"role": "user", "content":
                "раскрой эти данные PERSON_1, PERSON_3–PERSON_5"},
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
            {"role": "user", "content": "раскрой эти данные"},
        )
        cmd_bad = asyncio.run(handler.resolve_chat_command(req_bad))
        assert cmd_bad["command"] == "cmd_help", cmd_bad

        # Совместимость: «раскрой все данные» — прежняя полная деанонимизация
        req_old = _request(
            {"role": "assistant", "content": history},
            {"role": "user", "content": "раскрой все данные"},
        )
        cmd_old = asyncio.run(handler.resolve_chat_command(req_old))
        assert cmd_old["command"] == "deanon_files", cmd_old

        # Без слова «плейсхолдеры», но с токеном — частичная деанонимизация
        req_bare = _request(
            {"role": "assistant", "content": history},
            {"role": "user", "content": "раскрой PERSON_2"},
        )
        cmd_bare = asyncio.run(handler.resolve_chat_command(req_bare))
        assert cmd_bare["command"] == "deanon_placeholders", cmd_bare
        assert cmd_bare["tokens"] == ["[PERSON_2]"]

        # Регрессия 2026-09-10: разговорная форма «раскрой это PERSON_2»
        # (без слова «данные») — тоже выборочная деанонимизация
        req_this = _request(
            {"role": "assistant", "content": history},
            {"role": "user", "content": "раскрой это PERSON_2"},
        )
        cmd_this = asyncio.run(handler.resolve_chat_command(req_this))
        assert cmd_this["command"] == "deanon_placeholders", cmd_this
        assert cmd_this["tokens"] == ["[PERSON_2]"]
        assert cmd_this["targets"][0]["session_id"] == "sess-b1"

        # «раскрой это» без токенов — подсказка, а не полная деанонимизация
        req_this_bad = _request(
            {"role": "assistant", "content": history},
            {"role": "user", "content": "раскрой это"},
        )
        cmd_this_bad = asyncio.run(handler.resolve_chat_command(req_this_bad))
        assert cmd_this_bad["command"] == "cmd_help", cmd_this_bad
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
                "скрой эти данные: Иванов, Петрова"},
        )
        cmd = asyncio.run(handler.resolve_chat_command(req))
        assert cmd["command"] == "extra_anonymize", cmd
        assert cmd["names"] == ["Иванов", "Петрова"]
        assert cmd["targets"][0]["copy_path"] == str(copy)

        # Регрессия 2026-09-10: разговорная форма «Скрой это» из Hermes
        # (без слова «данные») должна резолвиться, а не уходить в облако
        req_this = _request(
            {"role": "assistant", "content": history},
            {"role": "user", "content": "Скрой это: Иванов, Петрова"},
        )
        cmd_this = asyncio.run(handler.resolve_chat_command(req_this))
        assert cmd_this["command"] == "extra_anonymize", cmd_this
        assert cmd_this["names"] == ["Иванов", "Петрова"]
        assert cmd_this["targets"][0]["copy_path"] == str(copy)

        # Остальные разговорные варианты («вот/только это»)
        for phrase in ("скрой вот это: Иванов",
                       "скрой только это: Петрова",
                       "Скрой это Иванов"):
            r2 = _request(
                {"role": "assistant", "content": history},
                {"role": "user", "content": phrase},
            )
            c2 = asyncio.run(handler.resolve_chat_command(r2))
            assert c2 and c2["command"] == "extra_anonymize", (phrase, c2)

        # Anti-fallthrough: без имён — подсказка, а не whole-file NER
        req_bad = _request(
            {"role": "assistant", "content": history},
            {"role": "user", "content": "скрой эти данные"},
        )
        cmd_bad = asyncio.run(handler.resolve_chat_command(req_bad))
        assert cmd_bad["command"] == "cmd_help", cmd_bad
        req_this_bad = _request(
            {"role": "assistant", "content": history},
            {"role": "user", "content": "скрой это"},
        )
        cmd_this_bad = asyncio.run(handler.resolve_chat_command(req_this_bad))
        assert cmd_this_bad["command"] == "cmd_help", cmd_this_bad

        # Обычная команда анонимизации файла — не новая команда
        req_plain = _request(
            {"role": "user", "content": "скрой все данные doc.docx"})
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
                    "скрой эти данные: Иванов, Петров, Сидоров"},
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
                    "раскрой эти данные PERSON_1, PERSON_3–PERSON_4"},
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
                    "раскрой эти данные PERSON_2"},
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


def test_roundtrip_extra_anonymize_arbitrary_values():
    """Регрессия 2026-09-09: дополнительная анонимизация ПРОИЗВОЛЬНЫХ
    строк (номер договора, дата) — не только имён; тип плейсхолдера MISC;
    точечная де-анонимизация по MISC-токену возвращает значение."""
    async def _run():
        with tempfile.TemporaryDirectory() as td:
            copy = Path(td) / "doc.anonymized.txt"
            copy.write_text(
                "Договор 0095/23/2.1/00075271/013/2023 от 27.12.2023, "
                "подписал Иванов.\n", encoding="utf-8")
            result = Path(td) / "doc.result.txt"
            store = FakeStoreWithLookup()
            handler = _make_handler(store)
            history = ("[ANONYMIZER] Файлы анонимизированы.\n"
                       "session_id: sess-c1\n"
                       f"[anonymizer:result:{result}]\n"
                       f"[anonymizer:copy:{copy}]")
            req = _request(
                {"role": "assistant", "content": history},
                {"role": "user", "content": (
                    "Скрой эти данные: "
                    "'0095/23/2.1/00075271/013/2023', '27.12.2023'")},
            )
            cmd = await handler.resolve_chat_command(req)
            assert cmd["command"] == "extra_anonymize", cmd
            assert cmd["names"] == [
                "0095/23/2.1/00075271/013/2023", "27.12.2023"], cmd["names"]
            response, kind = await handler.execute_chat_command(req, cmd)
            assert kind == "extra_anonymize"

            content = copy.read_text(encoding="utf-8")
            assert "0095/23/2.1/00075271/013/2023" not in content, content
            assert "27.12.2023" not in content, content
            assert "[MISC_1]" in content and "[MISC_2]" in content, content
            # Агент «перенёс» токенизированный текст в файл результата
            result.write_text(content, encoding="utf-8")
            text = response.choices[0].message.content
            # Нумерация токенов идёт с конца текста: дата → MISC_1,
            # номер → MISC_2
            assert "27.12.2023 → [MISC_1]" in text, text
            assert "0095/23/2.1/00075271/013/2023 → [MISC_2]" in text, text

            # Точечная де-анонимизация по MISC-токену возвращает значение
            req2 = _request(
                {"role": "assistant", "content": history},
                {"role": "user", "content":
                    "раскрой эти данные MISC_1"},
            )
            cmd2 = await handler.resolve_chat_command(req2)
            assert cmd2["command"] == "deanon_placeholders", cmd2
            await handler.execute_chat_command(req2, cmd2)
            result2 = result.read_text(encoding="utf-8")
            assert "27.12.2023" in result2, result2
            assert "[MISC_1]" not in result2, result2
            assert "[MISC_2]" in result2, result2  # номер не тронут
            assert "0095/23/2.1/00075271/013/2023" not in result2, result2
            # Копия осталась нетронутой (де-анонимизация — в файле результата)
            assert copy.read_text(encoding="utf-8") == content, copy
    asyncio.run(_run())
    print("TEST 10 OK: roundtrip extra_anonymize — произвольные строки, "
          "MISC-плейсхолдеры, точечная де-анонимизация")


def test_placeholders_deanon_without_dialog_markers():
    """Регрессия 2026-09-09: плейсхолдеры, созданные в ДРУГОМ чате,
    деанонимизируются в новом чате — цели ищутся в хранилище маппингов,
    даже если в диалоге нет маркеров [anonymizer:result:…]."""
    async def _run():
        with tempfile.TemporaryDirectory() as td:
            result = Path(td) / "doc.result.txt"
            result.write_text("Роль: [POSITION_1]\n[POSITION_2]",
                              encoding="utf-8")
            copy = Path(td) / "doc.anonymized.txt"
            copy.write_text("Роль: [POSITION_1]\n[POSITION_2]",
                            encoding="utf-8")
            store = FakeStoreWithLookup()
            # «Старый диалог»: маппинги и привязки файлов есть, маркеров
            # [anonymizer:result:…] в ТЕКУЩЕМ чате нет
            await store.add_mapping("sess-old", "Начальник отдела", "POSITION")
            await store.add_mapping(
                "sess-old", "Заместитель начальника", "POSITION")
            await store.register_file_session(str(result), "sess-old")
            await store.register_file_session(str(copy), "sess-old")
            handler = _make_handler(store)
            req = _request(
                {"role": "user", "content":
                    "раскрой эти данные POSITION_1-POSITION_2"},
            )
            cmd = await handler.resolve_chat_command(req)
            assert cmd["command"] == "deanon_placeholders", cmd
            response, kind = await handler.execute_chat_command(req, cmd)
            assert kind == "placeholders_deanonymization", kind
            text = response.choices[0].message.content
            assert "[POSITION_1] → Начальник отдела" in text, text
            assert "[POSITION_2] → Заместитель начальника" in text, text
            assert result.read_text(encoding="utf-8") == (
                "Роль: Начальник отдела\nЗаместитель начальника")
            assert copy.read_text(encoding="utf-8") == (
                "Роль: [POSITION_1]\n[POSITION_2]")  # копия не тронута
    asyncio.run(_run())
    print("TEST 11 OK: деанонимизация плейсхолдеров из другого чата — "
          "цели найдены в хранилище маппингов")


def test_roundtrip_extra_anonymize_both_files():
    """Вариант A: замены применяются к копии И файлу результата; значение,
    добавленное агентом только в result, заменяется там (с честной пометкой
    в отчёте)."""
    async def _run():
        with tempfile.TemporaryDirectory() as td:
            copy = Path(td) / "doc.anonymized.txt"
            copy.write_text("Договор подписал Иванов.\n", encoding="utf-8")
            result = Path(td) / "doc.result.txt"
            # Агент отредактировал result: добавил дату, которой нет в копии
            result.write_text(
                "Договор подписал Иванов. Дата: 27.12.2023.\n",
                encoding="utf-8")
            store = FakeStoreWithLookup()
            handler = _make_handler(store)
            history = ("[ANONYMIZER] Файлы анонимизированы.\n"
                       "session_id: sess-d1\n"
                       f"[anonymizer:result:{result}]\n"
                       f"[anonymizer:copy:{copy}]")
            req = _request(
                {"role": "assistant", "content": history},
                {"role": "user", "content":
                    "скрой эти данные: Иванов, 27.12.2023"},
            )
            cmd = await handler.resolve_chat_command(req)
            assert cmd["command"] == "extra_anonymize", cmd
            response, kind = await handler.execute_chat_command(req, cmd)
            assert kind == "extra_anonymize"

            copy_text = copy.read_text(encoding="utf-8")
            assert "Иванов" not in copy_text, copy_text
            assert "[PERSON_1]" in copy_text, copy_text
            assert "27.12.2023" not in copy_text  # в копии его и не было

            result_text = result.read_text(encoding="utf-8")
            assert "Иванов" not in result_text, result_text
            assert "27.12.2023" not in result_text, result_text
            assert "[MISC_1]" in result_text, result_text

            text = response.choices[0].message.content
            # Честная пометка про значение, которого нет в копии
            assert "в анонимизированной копии отсутствуют" in text, text
            assert "27.12.2023" in text, text
    asyncio.run(_run())
    print("TEST 12 OK: extra anonymize — замены в копии И результате, "
          "значение из result помечается честно")


def test_extra_anonymize_busy_file_honest_headers():
    """Регрессия 2026-09-10: занятый файл не должен «прятаться» за словом
    «выполнена» в заголовке ответа.

    1. Обе записи ломаются (PermissionError из os.replace) — заголовок
       «НЕ ВЫПОЛНЕНА», текст ошибки «занят», tmp-файл не остаётся.
    2. Копия записалась, result занят — заголовок «ЧАСТИЧНО» + пометка
       о неконсистентности.
    3. Значения не найдены вовсе — заголовок без ложного «заменены».
    """
    from unittest import mock
    from anonymizer_proxy.proxy import handlers as hmod

    async def _run():
        with tempfile.TemporaryDirectory() as td:
            copy = Path(td) / "doc.anonymized.txt"
            copy.write_text("Договор подписал Иванов.\n", encoding="utf-8")
            result = Path(td) / "doc.result.txt"
            result.write_text("Договор подписал Иванов.\n", encoding="utf-8")
            history = ("[ANONYMIZER] Файлы анонимизированы.\n"
                       "session_id: sess-e1\n"
                       f"[anonymizer:result:{result}]\n"
                       f"[anonymizer:copy:{copy}]")

            def _make_req():
                return _request(
                    {"role": "assistant", "content": history},
                    {"role": "user", "content": "скрой эти данные: Иванов"},
                )

            # --- 1. Полный провал: os.replace всегда PermissionError ---
            handler = _make_handler(FakeStoreWithLookup())
            req = _make_req()
            cmd = await handler.resolve_chat_command(req)
            assert cmd["command"] == "extra_anonymize", cmd
            real_replace = hmod.os.replace

            def _boom(src, dst):
                raise PermissionError(32, "Процесс не может получить доступ")

            with mock.patch.object(hmod.os, "replace", side_effect=_boom):
                response, _ = await handler.execute_chat_command(req, cmd)
            text = response.choices[0].message.content
            assert "НЕ ВЫПОЛНЕНА" in text, text
            assert "занят" in text, text
            assert "Word" in text, text
            # tmp-файлы не остались
            leftovers = list(Path(td).glob("*.anonymizer-tmp"))
            assert not leftovers, leftovers
            # Файлы не изменены (замены не применились)
            assert "Иванов" in copy.read_text(encoding="utf-8")
            assert "Иванов" in result.read_text(encoding="utf-8")
    asyncio.run(_run())
    print("TEST 13a OK: занятые файлы — заголовок «НЕ ВЫПОЛНЕНА», "
          "tmp не остаётся")


def test_extra_anonymize_partial_and_notfound_headers():
    """Часть 2 регрессии 2026-09-10: частичный успех и нулевой результат."""
    from unittest import mock
    from anonymizer_proxy.proxy import handlers as hmod

    async def _run():
        with tempfile.TemporaryDirectory() as td:
            copy = Path(td) / "doc.anonymized.txt"
            copy.write_text("Договор подписал Иванов.\n", encoding="utf-8")
            result = Path(td) / "doc.result.txt"
            result.write_text("Договор подписал Иванов.\n", encoding="utf-8")
            history = ("[ANONYMIZER] Файлы анонимизированы.\n"
                       "session_id: sess-e3\n"
                       f"[anonymizer:result:{result}]\n"
                       f"[anonymizer:copy:{copy}]")

            # --- 2. Частичный успех: копия ОК, result занят ---
            calls = {"n": 0}
            real_replace = hmod.os.replace

            def _flaky(src, dst):
                calls["n"] += 1
                if calls["n"] >= 2:  # вторая запись (result) падает
                    raise PermissionError(32, "занят")
                return real_replace(src, dst)

            handler2 = _make_handler(FakeStoreWithLookup())
            req2 = _request(
                {"role": "assistant", "content": history},
                {"role": "user", "content": "скрой эти данные: Иванов"},
            )
            cmd2 = await handler2.resolve_chat_command(req2)
            assert cmd2["command"] == "extra_anonymize", cmd2
            with mock.patch.object(hmod.os, "replace", side_effect=_flaky):
                resp2, _ = await handler2.execute_chat_command(req2, cmd2)
            text2 = resp2.choices[0].message.content
            assert "ЧАСТИЧНО" in text2, text2
            assert "НЕконсистентно" in text2, text2
            # Копия обновилась, result — нет
            assert "[PERSON_1]" in copy.read_text(encoding="utf-8")
            assert "Иванов" in result.read_text(encoding="utf-8")

            # --- 3. Значения не найдены — заголовок без ложного «заменены» ---
            copy3 = Path(td) / "x.anonymized.txt"
            copy3.write_text("Ничего релевантного.\n", encoding="utf-8")
            history3 = ("[ANONYMIZER] Файлы анонимизированы.\n"
                        "session_id: sess-e4\n"
                        f"[anonymizer:copy:{copy3}]")
            handler3 = _make_handler(FakeStoreWithLookup())
            req3 = _request(
                {"role": "assistant", "content": history3},
                {"role": "user", "content": "скрой эти данные: Петров"},
            )
            cmd3 = await handler3.resolve_chat_command(req3)
            resp3, _ = await handler3.execute_chat_command(req3, cmd3)
            text3 = resp3.choices[0].message.content
            assert "не найдено" in text3, text3
            assert "выполнена: указанные значения заменены" not in text3, text3

    asyncio.run(_run())
    print("TEST 13b OK: частичный успех («ЧАСТИЧНО») и нулевой результат "
          "(«не найдено») — честные заголовки")


def test_parse_name_list_decimal_merge():
    """Регрессия 2026-09-10: запятая внутри десятичного числа не должна
    рвать его на два значения списка."""
    names, errors = parse_name_list(
        ": 386887,42, 51903,14 и 7 432 480,00")
    assert names == ["386887,42", "51903,14", "7 432 480,00"], names
    assert errors == []
    # Обычные списки не склеиваются (первый элемент — не число)
    names2, _ = parse_name_list(": Иванов, 42, Петрова")
    assert names2 == ["Иванов", "42", "Петрова"], names2
    # Неоднозначный список можно задать через «;»
    names3, _ = parse_name_list(": 386887; 42")
    assert names3 == ["386887", "42"], names3
    print("TEST 14 OK: parse_name_list — десятичные числа не рвутся запятой")


def test_extra_anonymize_numbers_whole_replacement():
    """Регрессия 2026-09-10: числа из колонки XLSX скрываются ЦЕЛИКОМ.
    1. «386887» в тексте «386887,42» → матчится всё число (без обрубка).
    2. Форма пользователя «51 903,14» находит в файле «51903.14»
       (запятая/точка, разрядные пробелы)."""
    from anonymizer_proxy.models.schemas import ChatCompletionRequest
    from anonymizer_proxy.proxy import handlers as hmod

    async def _run():
        with tempfile.TemporaryDirectory() as td:
            copy = Path(td) / "doc.anonymized.txt"
            copy.write_text(
                "Итого: 386887,42 и 51903.14 подписал Иванов.\n",
                encoding="utf-8")
            history = ("[ANONYMIZER] Файлы анонимизированы.\n"
                       "session_id: sess-f1\n"
                       f"[anonymizer:copy:{copy}]")
            handler = _make_handler(FakeStoreWithLookup())
            req = _request(
                {"role": "assistant", "content": history},
                {"role": "user", "content":
                    "скрой эти данные: 386887,42, 51 903,14"},
            )
            cmd = await handler.resolve_chat_command(req)
            assert cmd["command"] == "extra_anonymize", cmd
            # parse_name_list не порвал десятичные числа запятой
            assert cmd["names"] == ["386887,42", "51 903,14"], cmd["names"]
            response, _ = await handler.execute_chat_command(req, cmd)
            text = copy.read_text(encoding="utf-8")
            assert "386887" not in text and "51903.14" not in text, text
            assert "[MISC_" in text, text
            assert "Иванов" in text  # не из списка — не тронут
            # в отчёте перечислены полные значения
            report = response.choices[0].message.content
            assert "386887,42" in report, report
            assert "51903.14" in report, report

            # Сценарий 2: пользователь назвал только целую часть —
            # скрывается ВСЁ число (без обрубка «386887.[MISC_5]»)
            copy2 = Path(td) / "doc2.anonymized.txt"
            copy2.write_text("Сумма: 386887,42. Конец.\n", encoding="utf-8")
            history2 = ("[ANONYMIZER] Файлы анонимизированы.\n"
                        "session_id: sess-f2\n"
                        f"[anonymizer:copy:{copy2}]")
            handler2 = _make_handler(FakeStoreWithLookup())
            req2 = _request(
                {"role": "assistant", "content": history2},
                {"role": "user", "content": "скрой эти данные: 386887"},
            )
            cmd2 = await handler2.resolve_chat_command(req2)
            assert cmd2["command"] == "extra_anonymize", cmd2
            assert cmd2["names"] == ["386887"], cmd2["names"]
            await handler2.execute_chat_command(req2, cmd2)
            text2 = copy2.read_text(encoding="utf-8")
            assert "386887" not in text2, text2
            assert "[MISC_" in text2, text2
            assert "Конец" in text2  # окружение не тронутo
    asyncio.run(_run())
    print("TEST 15 OK: числа из «скрой эти данные» заменяются целиком, "
          "форма запятая/точка/пробелы не важна")


if __name__ == "__main__":
    test_parse_placeholder_spec_lists_and_ranges()
    test_parse_placeholder_spec_range_forms()
    test_parse_placeholder_spec_errors()
    test_parse_name_list()
    test_parse_name_list_garbage()
    test_resolve_placeholder_deanon()
    test_resolve_extra_anonymize()
    test_roundtrip_extra_anonymize()
    test_roundtrip_extra_anonymize_arbitrary_values()
    test_roundtrip_placeholders_partial_deanon()
    test_placeholders_deanon_without_dialog_markers()
    test_roundtrip_extra_anonymize_both_files()
    test_extra_anonymize_busy_file_honest_headers()
    test_extra_anonymize_partial_and_notfound_headers()
    test_parse_name_list_decimal_merge()
    test_extra_anonymize_numbers_whole_replacement()
    print("\nALL EXTRA COMMANDS TESTS PASSED")


