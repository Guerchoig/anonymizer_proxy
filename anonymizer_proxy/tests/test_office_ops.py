"""
Функциональные тесты office_ops — CLI-инструментария правки DOCX/XLSX.

Проверяют, что облачная модель может менять документы готовыми командами
(без python-docx скриптов): обзор структуры, dump/apply round-trip,
точечные замены с сохранением форматирования, правка ячеек, добавление
строк и столбцов (DOCX), значения и строки (XLSX), а также инъекцию
«шпаргалки» office_ops в системный промпт passthrough-прокси.

Запуск: python anonymizer_proxy\\tests\\test_office_ops.py (из корня проекта)
"""
import asyncio
import io
import subprocess
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

from docx import Document
from openpyxl import Workbook, load_workbook

from anonymizer_proxy.models.schemas import ChatCompletionRequest, ChatMessage
from anonymizer_proxy.proxy import handlers as handlers_module
from anonymizer_proxy.proxy.handlers import RequestHandler
from anonymizer_proxy.tests.test_tools_passthrough import (
    FakeNER, FakeStore, FakeOpenRouter,
)

handlers_module.CURRENT_MODE = "passthrough"

CLOUD_RESPONSE = {
    "id": "c-1", "created": 1, "model": "m",
    "choices": [{
        "index": 0,
        "message": {"role": "assistant", "content": "ok"},
        "finish_reason": "stop",
    }],
    "usage": {},
}


def run_cli(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "anonymizer_proxy.office_ops", *argv],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


def make_docx() -> Path:
    doc = Document()
    doc.add_paragraph("Вводная часть договора подряда")
    table = doc.add_table(rows=3, cols=3)
    data = [
        ["Наименование", "Кол-во", "Цена"],
        ["Кабель", "10", "100"],
        ["Крепёж", "5", "50"],
    ]
    for r, row in enumerate(data):
        for c, value in enumerate(row):
            table.rows[r].cells[c].text = value
    tmp = tempfile.NamedTemporaryFile(
        suffix=".docx", delete=False)
    tmp.close()
    doc.save(tmp.name)
    return Path(tmp.name)


def make_xlsx() -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "Смета"
    ws.append(["Позиция", "Сумма"])
    ws.append(["Работы", 1000])
    tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
    tmp.close()
    wb.save(tmp.name)
    return Path(tmp.name)


def test_list_tables_and_dump() -> None:
    """list-tables и dump показывают структуру и текст документа"""
    path = make_docx()
    try:
        proc = run_cli("list-tables", "--file", str(path))
        assert proc.returncode == 0, proc.stderr
        assert "Таблица 0: 3 строк x 3 столбцов" in proc.stdout, proc.stdout
        assert "Наименование" in proc.stdout

        proc = run_cli("dump", "--file", str(path), "--format", "md")
        assert proc.returncode == 0, proc.stderr
        assert "| Наименование | Кол-во | Цена |" in proc.stdout, proc.stdout
        print("TEST 1 OK: list-tables и dump (text/md)")
    finally:
        path.unlink(missing_ok=True)


def test_replace_text_preserves_original() -> None:
    """replace-text: замена в новый файл, исходник не тронут"""
    path = make_docx()
    out = path.with_name("replaced.docx")
    try:
        proc = run_cli("replace-text", "--file", str(path),
                       "--find", "Кабель", "--replace", "Провод",
                       "--output", str(out))
        assert proc.returncode == 0, proc.stderr
        assert "заменено вхождений: 1" in proc.stdout, proc.stdout
        assert "Провод" not in Document(path).tables[0].rows[1].cells[0].text, \
            "исходник изменён без --in-place"
        assert "Провод" in Document(out).tables[0].rows[1].cells[0].text
        print("TEST 2 OK: replace-text — в отдельный файл, исходник цел")
    finally:
        path.unlink(missing_ok=True)
        out.unlink(missing_ok=True)


def test_set_cell_add_row_add_column() -> None:
    """set-cell / add-row / add-column меняют таблицы DOCX"""
    path = make_docx()
    out = path.with_name("edited.docx")
    try:
        proc = run_cli("set-cell", "--file", str(path),
                       "--table", "0", "--row", "1", "--col", "2",
                       "--text", "120", "--output", str(out))
        assert proc.returncode == 0, proc.stderr
        doc = Document(out)
        assert doc.tables[0].rows[1].cells[2].text == "120"

        proc = run_cli("add-row", "--file", str(out), "--table", "0",
                       "--cell", "Лампы", "--cell", "2", "--cell", "30",
                       "--output", str(out))
        assert proc.returncode == 0, proc.stderr
        doc = Document(out)
        assert len(doc.tables[0].rows) == 4
        assert doc.tables[0].rows[3].cells[0].text == "Лампы"

        proc = run_cli("add-row", "--file", str(out), "--table", "0",
                       "--cell", "ПЕРВАЯ", "--position", "1",
                       "--output", str(out))
        assert proc.returncode == 0, proc.stderr
        doc = Document(out)
        assert len(doc.tables[0].rows) == 5
        assert doc.tables[0].rows[1].cells[0].text == "ПЕРВАЯ"
        assert doc.tables[0].rows[2].cells[0].text == "Кабель"

        proc = run_cli("add-column", "--file", str(out), "--table", "0",
                       "--header", "Итого", "--cell", "120", "--cell", "250",
                       "--cell", "150", "--cell", "60", "--cell", "30",
                       "--output", str(out))
        assert proc.returncode == 0, proc.stderr
        doc = Document(out)
        assert len(doc.tables[0].columns) == 4
        assert doc.tables[0].rows[0].cells[3].text == "Итого"
        assert doc.tables[0].rows[2].cells[3].text == "250"
        print("TEST 3 OK: set-cell / add-row (конец и вставка) / add-column")
    finally:
        path.unlink(missing_ok=True)
        out.unlink(missing_ok=True)


def test_apply_roundtrip() -> None:
    """dump -> правка строки -> apply: правка доходит до документа"""
    path = make_docx()
    out = path.with_name("applied.docx")
    edit = path.with_name("edit.txt")
    try:
        proc = run_cli("dump", "--file", str(path))
        assert proc.returncode == 0, proc.stderr
        segments = proc.stdout.rstrip("\n").split("\n")
        assert any("Крепёж" in s for s in segments), segments

        # Модель правит только нужную строку сегментов
        edited = "\n".join(
            s.replace("Крепёж", "Метизы") if "Крепёж" in s else s
            for s in segments
        )
        edit.write_text(edited, encoding="utf-8")

        proc = run_cli("apply", "--file", str(path),
                       "--from-text", str(edit), "--output", str(out))
        assert proc.returncode == 0, proc.stderr

        doc = Document(out)
        assert doc.tables[0].rows[2].cells[0].text == "Метизы"
        assert doc.tables[0].rows[1].cells[0].text == "Кабель"
        assert "Вводная часть договора подряда" in doc.paragraphs[0].text

        # Несовпадение числа строк — явная ошибка, а не тихая порча
        edit.write_text(edited + "\nлишняя строка", encoding="utf-8")
        proc = run_cli("apply", "--file", str(path),
                       "--from-text", str(edit), "--output", str(out))
        assert proc.returncode != 0
        assert "не совпадает" in (proc.stderr + proc.stdout)
        print("TEST 4 OK: apply round-trip + защита от рассинхрона строк")
    finally:
        path.unlink(missing_ok=True)
        out.unlink(missing_ok=True)
        edit.unlink(missing_ok=True)


def test_xlsx_set_value_append_row() -> None:
    """set-value / append-row пишут в XLSX, числа остаются числами"""
    path = make_xlsx()
    out = path.with_name("sheet_edited.xlsx")
    try:
        proc = run_cli("set-value", "--file", str(path),
                       "--cell", "B3", "--value", "2500",
                       "--output", str(out))
        assert proc.returncode == 0, proc.stderr
        ws = load_workbook(out).active
        assert ws["B3"].value == 2500, ws["B3"].value
        assert ws["A1"].value == "Позиция"  # остальное не тронуто

        proc = run_cli("append-row", "--file", str(out),
                       "--cell", "Материалы", "--cell", "500",
                       "--output", str(out))
        assert proc.returncode == 0, proc.stderr
        ws = load_workbook(out).active
        assert ws["A4"].value == "Материалы"
        assert ws["B4"].value == 500

        proc = run_cli("set-value", "--file", str(out), "--cell", "C1",
                       "--value", "007", "--as-text", "--output", str(out))
        assert proc.returncode == 0, proc.stderr
        ws = load_workbook(out).active
        assert ws["C1"].value == "007"
        print("TEST 5 OK: set-value / append-row / --as-text (XLSX)")
    finally:
        path.unlink(missing_ok=True)
        out.unlink(missing_ok=True)


def make_office_request(path: Path, text: str) -> ChatCompletionRequest:
    content = (
        f'{text}\n\n'
        f'<file_content path="{path}">\n'
        f'Error fetching content: binary file\n'
        f'</file_content>'
    )
    return ChatCompletionRequest(
        model="m", anonymize=True, stream=False,
        messages=[ChatMessage(role="user", content=content)],
    )


def test_office_ops_hint_injection() -> None:
    """Шпаргалка office_ops добавляется в системный промпт для .docx/.xlsx
    и НЕ добавляется для прочих файлов"""
    docx_path = make_docx()
    txt_path = None
    try:
        handler = RequestHandler(
            ner_service=FakeNER({}),
            mapping_store=FakeStore(),
            openrouter_client=FakeOpenRouter(CLOUD_RESPONSE),
        )
        resp, _ = asyncio.run(handler.handle_chat_completion(
            make_office_request(docx_path, "Добавь колонку в таблицы")
        ))
        assert handler.openrouter.captured, "облако не вызвано"
        msgs = handler.openrouter.captured["messages"]
        assert msgs[0]["role"] == "system", msgs[0]["role"]
        assert "office_ops" in msgs[0]["content"], msgs[0]["content"][:200]
        assert "python -m anonymizer_proxy.office_ops" in msgs[0]["content"]
        assert resp.anonymization_metadata["mode"] == "passthrough"

        tmp = tempfile.NamedTemporaryFile(
            suffix=".txt", delete=False, mode="w", encoding="utf-8")
        tmp.write("обычный текст")
        tmp.close()
        txt_path = Path(tmp.name)
        handler2 = RequestHandler(
            ner_service=FakeNER({}),
            mapping_store=FakeStore(),
            openrouter_client=FakeOpenRouter(CLOUD_RESPONSE),
        )
        asyncio.run(handler2.handle_chat_completion(
            make_office_request(txt_path, "Составь резюме")
        ))
        msgs2 = handler2.openrouter.captured["messages"]
        assert msgs2[0]["role"] == "user", "для .txt шпаргалка не нужна"
        print("TEST 6 OK: шпаргалка office_ops — в системный промпт только для Office")
    finally:
        docx_path.unlink(missing_ok=True)
        if txt_path:
            txt_path.unlink(missing_ok=True)


def test_add_column_all_tables() -> None:
    """--table all: одна команда добавляет колонку во ВСЕ таблицы документа
    (защита от сценария «цепочка команд затёрла записи»)"""
    doc = Document()
    doc.add_paragraph("Заголовок документа")
    doc.add_table(rows=2, cols=1)
    doc.add_table(rows=3, cols=2)
    doc.add_table(rows=1, cols=4)
    tmp = tempfile.NamedTemporaryFile(suffix=".docx", delete=False)
    tmp.close()
    doc.save(tmp.name)
    path = Path(tmp.name)
    out = path.with_name("all_cols.docx")
    try:
        proc = run_cli("add-column", "--file", str(path),
                       "--table", "all", "--header", "Комментарий",
                       "--output", str(out))
        assert proc.returncode == 0, proc.stderr
        assert "во все 3 таблиц" in proc.stdout, proc.stdout

        res = Document(out)
        assert len(res.tables) == 3
        for i, table in enumerate(res.tables):
            grid = table._tbl.find(
                "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}tblGrid")
            ncols = len(grid)
            orig_cols = [1, 2, 4][i]
            assert ncols == orig_cols + 1, (i, ncols)
            for row in table.rows:
                ntc = len(row._tr.findall(
                    "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}tc"))
                assert ntc == ncols, (i, ntc, ncols)
            assert table.rows[0].cells[-1].text == "Комментарий"
        print("TEST 7 OK: add-column --table all — колонка во всех таблицах одной командой")
    finally:
        path.unlink(missing_ok=True)
        out.unlink(missing_ok=True)


def test_office_ops_hint_client_agnostic() -> None:
    """Шпаргалка office_ops срабатывает и БЕЗ блоков <file_content> (Cline):
    по упоминанию .docx/.xlsx в тексте сообщения и в аргументах tool_calls
    (другие агенты, например Hermes), а также содержит пункт о приоритете
    над skills/инструментами агента."""
    inject = RequestHandler._inject_office_ops_hint

    # 1. Путь в тексте user-сообщения (кириллица и пробелы в имени —
    #    как у реальных файлов), блоков <file_content> нет
    msgs = [ChatMessage(
        role="user",
        content="Проверь отчёт КП ДО 01.docx и добавь колонку в таблицы",
    )]
    out = inject(msgs)
    assert out[0].role == "system", out[0].role
    assert "python -m anonymizer_proxy.office_ops" in out[0].content
    assert "ВЫСШИЙ приоритет" in out[0].content

    # 2. Путь только в аргументах tool_calls (стиль агентов вида Hermes:
    #    read_file/execute_code с путем к xlsx), content пустой
    msgs = [
        ChatMessage(role="user", content="Обнови данные в Excel"),
        ChatMessage(
            role="assistant",
            content=None,
            tool_calls=[{
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "read_file",
                    "arguments": '{"path": "C:\\Test\\отчёт.xlsx"}',
                },
            }],
        ),
    ]
    out = inject(msgs)
    assert out[0].role == "system", out[0].role
    assert "office_ops" in out[0].content

    # 3. Без Office-файлов шпаргалка не добавляется
    msgs = [ChatMessage(role="user", content="Составь резюме заметки.md")]
    out = inject(msgs)
    assert out[0].role == "user", "шпаргалка не нужна без .docx/.xlsx"

    # 4. Голое упоминание расширения («форматы .docx») — не триггер:
    #    перед расширением требуются непробельные символы (имя/путь файла)
    msgs = [ChatMessage(role="user", content="Какие форматы .docx вы читаете?")]
    out = inject(msgs)
    assert out[0].role == "user", "упоминание расширения без файла — не триггер"

    print("TEST 8 OK: шпаргалка office_ops — клиенто-независимый триггер "
          "(текст/tool_calls) и приоритет над skills")


def test_write_guard_anonymized_copy() -> None:
    """Правило исходника: запись в <name>.anonymized.<ext> запрещена CLI;
    повторная команда с --file <копия> при существующем результате получает
    предупреждение о перезапуске цепочки (предыдущие правки будут затёрты)."""
    tmp = tempfile.NamedTemporaryFile(suffix=".docx", delete=False)
    tmp.close()
    path = Path(tmp.name)
    copy = path.with_name(path.stem + ".anonymized.docx")
    result = path.with_name(path.stem + ".result.docx")
    try:
        doc = Document()
        doc.add_paragraph("Вводная часть договора подряда")
        doc.save(copy)

        # 1. Запись в саму копию (--output = копия) — запрещена
        proc = run_cli("replace-text", "--file", str(copy),
                       "--find", "Вводная", "--replace", "X",
                       "--output", str(copy))
        assert proc.returncode != 0, proc.stdout
        assert "ЗАПРЕЩЕНО" in proc.stderr, proc.stderr

        # 2. --in-place на копии — запрещён
        proc = run_cli("replace-text", "--file", str(copy),
                       "--find", "Вводная", "--replace", "X",
                       "--in-place")
        assert proc.returncode != 0, proc.stdout
        assert "ЗАПРЕЩЕНО" in proc.stderr, proc.stderr

        # 3. Первая правка: копия -> результат — разрешена
        proc = run_cli("replace-text", "--file", str(copy),
                       "--find", "Вводная", "--replace", "Первая",
                       "--output", str(result))
        assert proc.returncode == 0, proc.stderr
        assert not proc.stderr.strip(), proc.stderr
        assert "Первая часть" in Document(result).paragraphs[0].text

        # 4. Повторная команда с --file <копия> при существующем результате —
        #    выполняется, но с предупреждением о перезапуске цепочки
        proc = run_cli("replace-text", "--file", str(copy),
                       "--find", "Вводная", "--replace", "Вторая",
                       "--output", str(result))
        assert proc.returncode == 0, proc.stderr
        assert "ЦЕПОЧКУ ЗАНОВО" in proc.stderr, proc.stderr
        assert "затрёт предыдущие правки" in proc.stderr, proc.stderr
        assert "Вторая часть" in Document(result).paragraphs[0].text
        print("TEST 9 OK: запрет записи в .anonymized-копию + предупреждение "
              "о перезапуске цепочки правок")
    finally:
        for p in (path, copy, result):
            Path(str(p) + ".tmp").unlink(missing_ok=True)
            p.unlink(missing_ok=True)


def test_office_edit_chain_hint() -> None:
    """Динамический блок «цепочка правок» в шпаргалке: по маркерам
    [anonymizer:result:...] истории модель получает КОНКРЕТНОЕ указание
    продолжать правки в существующем файле результата."""
    inject = RequestHandler._inject_office_ops_hint
    docx_tmp = tempfile.NamedTemporaryFile(suffix=".docx", delete=False)
    docx_tmp.close()
    docx = Path(docx_tmp.name)
    result = docx.with_name(docx.stem + ".result.docx")
    try:
        marker = f"[anonymizer:result:{result}]"
        base = [
            ChatMessage(role="system", content="Ты ассистент."),
            ChatMessage(role="assistant",
                        content=f"[ANONYMIZER] Готово.\n{marker}"),
            ChatMessage(role="user", content=(
                f'<file_content path="{docx}">binary</file_content> '
                "добавь колонку")),
        ]

        # 1. Файл результата существует -> «УЖЕ существует» + путь
        result.write_bytes(b"stub")
        out = inject(list(base))
        assert out[0].role == "system"
        assert "[OFFICE-OPS/ЦЕПОЧКА]" in out[0].content
        assert "УЖЕ существует" in out[0].content
        assert str(result) in out[0].content
        assert "ЗАПРЕЩЕНО" in out[0].content

        # 2. Маркер есть, но файла ещё нет -> «ещё не создан»
        result.unlink()
        out = inject(list(base))
        assert "[OFFICE-OPS/ЦЕПОЧКА]" in out[0].content
        assert "ещё не создан" in out[0].content

        # 3. Без маркеров результата сессионного блока нет (шпаргалка
        #    вставляется отдельным system-сообщением)
        out = inject([ChatMessage(role="user", content=(
            f'<file_content path="{docx}">binary</file_content> добавь колонку'))])
        assert out[0].role == "system"
        assert "office_ops" in out[0].content
        assert "[OFFICE-OPS/ЦЕПОЧКА]" not in out[0].content
        print("TEST 10 OK: динамический блок «цепочка правок» по маркерам "
              "[anonymizer:result:]")
    finally:
        docx.unlink(missing_ok=True)
        result.unlink(missing_ok=True)


def test_read_column_and_delete_column() -> None:
    """read-column: значения колонки с адресами ячеек (даты ISO, шапка
    журнала не в первой строке); delete-column: удаление столбца XLSX/DOCX
    в файл результата — исходник не тронут, даты не теряются."""
    # --- XLSX: шапка в 4-й строке, как в реальных журналах контроля ---
    xlsx = Path(tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False).name)
    wb = Workbook()
    ws = wb.active
    ws.title = "ЖКП"
    ws.append(["Журнал контроля поручений"])
    ws.append([]), ws.append([])
    ws.append(["№", "Наименование", "Срок поручения", "Комментарий"])
    ws.cell(row=5, column=1, value="1.1")
    ws.cell(row=5, column=3, value=date(2026, 5, 22))
    ws.cell(row=5, column=4, value="ok")
    ws.cell(row=6, column=1, value="1.2")
    ws.cell(row=6, column=3, value=date(2026, 5, 15))
    wb.save(xlsx)
    xlsx_out = xlsx.with_name(xlsx.stem + ".result.xlsx")
    copy = None  # временная .anonymized.-копия для проверки запрета записи
    docx = docx_out = None
    try:
        # 1. Чтение по тексту заголовка: адрес + ISO-дата, без 00:00:00
        proc = run_cli("read-column", "--file", str(xlsx),
                       "--column", "Срок поручения")
        assert proc.returncode == 0, proc.stderr
        assert "колонка C (Срок поручения), заголовок в строке 4" in proc.stdout, proc.stdout
        assert "C5 (строка 5): 2026-05-22" in proc.stdout, proc.stdout
        assert "C6 (строка 6): 2026-05-15" in proc.stdout, proc.stdout
        assert "00:00:00" not in proc.stdout, proc.stdout
        assert "A5 (строка 5):" not in proc.stdout  # только целевая колонка

        # 2. Чтение по букве столбца; пустые ячейки пропускаются
        proc = run_cli("read-column", "--file", str(xlsx), "--column", "D")
        assert "D5 (строка 5): ok" in proc.stdout, proc.stdout
        assert "D6" not in proc.stdout, proc.stdout

        # 3. Незнакомый заголовок — явная ошибка
        proc = run_cli("read-column", "--file", str(xlsx), "--column", "Нет такого")
        assert proc.returncode != 0 and "не найден" in proc.stderr, proc.stderr

        # 4. Удаление по заголовку — в файл результата, исходник цел
        proc = run_cli("delete-column", "--file", str(xlsx),
                       "--column", "Комментарий", "--output", str(xlsx_out))
        assert proc.returncode == 0, proc.stderr
        ws_out = load_workbook(xlsx_out).active
        assert ws_out.max_column == 3, ws_out.max_column
        assert ws_out.cell(row=4, column=3).value == "Срок поручения"
        assert ws_out.cell(row=5, column=3).value == datetime(2026, 5, 22)
        assert load_workbook(xlsx).active.max_column == 4  # исходник не тронут

        # 5. Запись в анонимизированную копию по-прежнему запрещена
        copy = xlsx.with_name(xlsx.stem + ".anonymized.xlsx")
        copy.write_bytes(xlsx.read_bytes())
        proc = run_cli("delete-column", "--file", str(copy),
                       "--column", "Комментарий", "--in-place")
        assert proc.returncode != 0 and "ЗАПРЕЩЕНО" in proc.stderr, proc.stderr

        # --- DOCX: чтение и удаление столбца таблицы ---
        docx = make_docx()
        docx_out = docx.with_name(docx.stem + ".result.docx")
        proc = run_cli("read-column", "--file", str(docx),
                       "--table", "0", "--column", "1")
        assert proc.returncode == 0, proc.stderr
        assert "строка 1: 10" in proc.stdout, proc.stdout
        assert "Таблица 0, колонка 1" in proc.stdout, proc.stdout

        proc = run_cli("delete-column", "--file", str(docx),
                       "--table", "0", "--column", "2", "--output", str(docx_out))
        assert proc.returncode == 0, proc.stderr
        t_out = Document(docx_out).tables[0]
        assert len(t_out.columns) == 2 and t_out.rows[0].cells[1].text == "Кол-во"
        assert len(Document(docx).tables[0].columns) == 3  # исходник не тронут
        print("TEST 12 OK: read-column / delete-column (XLSX и DOCX)")
    finally:
        copy = xlsx.with_name(xlsx.stem + ".anonymized.xlsx")
        for p in (xlsx, xlsx_out, copy, docx, docx_out,
                  Path(str(xlsx) + ".tmp"), Path(str(xlsx_out) + ".tmp")):
            if p is not None:
                p.unlink(missing_ok=True)


def test_xlsx_add_column() -> None:
    """add-column (XLSX): вставка столбца после целевого по заголовку,
    ISO-даты становятся датами, merge-шапка сдвигается, исходник не тронут."""
    xlsx = Path(tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False).name)
    wb = Workbook()
    ws = wb.active
    ws.title = "ЖКП"
    ws["B1"] = "Журнал"
    ws.merge_cells("B1:D1")  # шапка-заголовок на всю ширину, как в журналах
    for c, v in enumerate(
            ["№", "Наименование", "Срок поручения", "Комментарий"], start=1):
        ws.cell(row=4, column=c, value=v)
    ws.cell(row=5, column=1, value="1.1")
    ws.cell(row=5, column=3, value=date(2026, 5, 22))
    ws.cell(row=5, column=4, value="ok")
    ws.cell(row=6, column=1, value="1.2")
    ws.cell(row=6, column=3, value=date(2026, 5, 15))
    wb.save(xlsx)
    out = xlsx.with_name(xlsx.stem + ".result.xlsx")
    try:
        # 1. Вставка ПОСЛЕ «Срок поручения» со значениями-датами
        proc = run_cli(
            "add-column", "--file", str(xlsx),
            "--column", "Срок поручения", "--position", "after",
            "--header", "Поручение выдано",
            "--cell", "2026-05-19", "--cell", "2026-05-12",
            "--output", str(out))
        assert proc.returncode == 0, proc.stderr
        assert "столбец D «Поручение выдано»" in proc.stdout, proc.stdout
        assert "строка заголовка: 4" in proc.stdout, proc.stdout
        ws_out = load_workbook(out).active
        assert ws_out.max_column == 5  # было 4 (A..D), вставка после C — 5
        assert ws_out.cell(row=4, column=4).value == "Поручение выдано"
        assert ws_out.cell(row=4, column=5).value == "Комментарий"
        d5 = ws_out.cell(row=5, column=4).value
        assert d5 == datetime(2026, 5, 19), d5
        assert "YY" in ws_out.cell(row=5, column=4).number_format.upper() or \
               "DD" in ws_out.cell(row=5, column=4).number_format.upper()
        assert ws_out.cell(row=6, column=4).value == datetime(2026, 5, 12)
        # шапка-merge расширлась: B1:D1 -> B1:E1
        assert "B1:E1" in [str(m) for m in ws_out.merged_cells.ranges], \
            [str(m) for m in ws_out.merged_cells.ranges]
        # исходник не тронут
        ws_src = load_workbook(xlsx).active
        assert ws_src.max_column == 4 and ws_src.cell(row=4, column=4).value == "Комментарий"
        assert "B1:D1" in [str(m) for m in ws_src.merged_cells.ranges]

        # 2. Вставка ДО целевого столбца (merge левее точки вставки сдвигается)
        proc = run_cli(
            "add-column", "--file", str(out),
            "--column", "Наименование", "--position", "before",
            "--header", "Отметка", "--output", str(out), "--in-place")
        assert proc.returncode == 0, proc.stderr
        ws2 = load_workbook(out).active
        assert ws2.cell(row=4, column=2).value == "Отметка"
        assert ws2.cell(row=4, column=3).value == "Наименование"
        assert ws2.max_column == 6
        assert "C1:F1" in [str(m) for m in ws2.merged_cells.ranges], \
            [str(m) for m in ws2.merged_cells.ranges]

        # 3. Без --column — столбец в конец листа, шапка найдена в строке 4
        proc = run_cli(
            "add-column", "--file", str(out), "--header", "Итог",
            "--output", str(out), "--in-place")
        assert proc.returncode == 0, proc.stderr
        assert "строка заголовка: 4" in proc.stdout, proc.stdout
        ws3 = load_workbook(out).active
        assert ws3.cell(row=4, column=7).value == "Итог"
        print("TEST 13 OK: add-column (XLSX) — вставка по ориентиру, даты, merge")
    finally:
        for p in (xlsx, out, Path(str(xlsx) + ".tmp"), Path(str(out) + ".tmp")):
            p.unlink(missing_ok=True)


def test_guard_result_naming_and_dates() -> None:
    """Защита имён результата: чтение/запись «….xlsx.result» даёт понятную
    ошибку с правильным именем и НЕ создаёт мусорный файл; add-column
    (XLSX) отклоняет --table с подсказкой; даты DD.MM.YYYY приводятся к
    датам; без --column строка заголовков определяется автоматически."""
    xlsx = Path(tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False).name)
    wb = Workbook()
    ws = wb.active
    ws.title = "ЖКП"
    ws["B1"] = "Журнал"
    ws.merge_cells("B1:D1")
    for c, v in enumerate(
            ["№", "Наименование", "Срок поручения", "Комментарий"], start=1):
        ws.cell(row=4, column=c, value=v)
    ws.cell(row=5, column=1, value="1.1")
    ws.cell(row=5, column=3, value=date(2026, 5, 22))
    wb.save(xlsx)
    bad = xlsx.with_name(xlsx.stem + ".xlsx.result")  # неверное имя результата
    bad.write_bytes(xlsx.read_bytes())
    out = xlsx.with_name(xlsx.stem + ".result.xlsx")
    try:
        # 1. Чтение файла с неверным именем — ошибка + правильное имя
        proc = run_cli("read-column", "--file", str(bad), "--column", "A")
        assert proc.returncode != 0, proc.stdout
        assert "неперепутано" not in proc.stderr  # san: текст сообщения ниже
        assert "результата перепутано" in proc.stderr, proc.stderr
        assert out.name in proc.stderr and bad.name in proc.stderr, proc.stderr

        # 2. Запись в неверное имя — отказ, подсказка, мусор не создан
        proc = run_cli("add-column", "--file", str(xlsx), "--header", "X",
                       "--output", str(bad))
        assert proc.returncode != 0 and "Неверное имя файла результата" in proc.stderr, proc.stderr
        assert out.name in proc.stderr, proc.stderr
        assert not out.exists()  # nothing written to the correct name

        # 3. --table для XLSX — явная ошибка вместо молчаливого игнорирования
        proc = run_cli("add-column", "--file", str(xlsx), "--table", "all",
                       "--header", "X", "--output", str(out))
        assert proc.returncode != 0 and "--table" in proc.stderr \
            and "DOCX" in proc.stderr, proc.stderr
        assert not out.exists()

        # 4. Даты DD.MM.YYYY -> настоящие даты; без --column шапка ищется
        #    автоматически (заголовок в строке 4, а не в 1-й)
        proc = run_cli("add-column", "--file", str(xlsx),
                       "--header", "Поручение выдано",
                       "--cell", "15.05.2026", "--cell", "19.05.2026",
                       "--output", str(out))
        assert proc.returncode == 0, proc.stderr
        assert "строка заголовка: 4" in proc.stdout, proc.stdout
        ws_out = load_workbook(out).active
        assert ws_out.cell(row=4, column=5).value == "Поручение выдано"
        assert ws_out.cell(row=5, column=5).value == datetime(2026, 5, 15)
        assert ws_out.cell(row=6, column=5).value == datetime(2026, 5, 19)
        print("TEST 14 OK: имена результата, --table (XLSX), DD.MM.YYYY, авто-шапка")
    finally:
        for p in (xlsx, bad, out, Path(str(xlsx) + ".tmp"),
                  Path(str(out) + ".tmp")):
            p.unlink(missing_ok=True)


def test_insert_text_multiline() -> None:
    """insert-text: переносы внутри --text (настоящие и литеральные \\n)
    делят текст на отдельные абзацы — многострочное резюме не склеивается
    в один абзац; apply с добавленными строками подсказывает insert-text."""
    docx = Path(tempfile.NamedTemporaryFile(suffix=".docx", delete=False).name)
    Document().save(docx)
    out = docx.with_name(docx.stem + ".result.docx")
    try:
        # 1. Литеральные \\n (как их присылает модель через PowerShell)
        proc = run_cli("insert-text", "--file", str(docx),
                       "--text", "========================================\\n"
                                 "РЕЗЮМЕ ДОКУМЕНТА\\n"
                                 "========================================",
                       "--output", str(out))
        assert proc.returncode == 0, proc.stderr
        assert "вставлено абзацев: 3" in proc.stdout, proc.stdout
        paras = [p.text for p in Document(out).paragraphs]
        assert paras[-3:] == ["========================================",
                              "РЕЗЮМЕ ДОКУМЕНТА",
                              "========================================"], paras[-3:]
        assert "\\n" not in paras[-1], "литеральные \\n не должны попасть в текст"

        # 2. Настоящие переносы + смешение с обычными --text
        proc = run_cli("insert-text", "--file", str(out),
                       "--text", "Первый абзац\\nВторой абзац",
                       "--text", "Третий абзац", "--in-place")
        assert proc.returncode == 0 and "вставлено абзацев: 3" in proc.stdout, proc.stdout
        paras = [p.text for p in Document(out).paragraphs]
        assert paras[-3:] == ["Первый абзац", "Второй абзац", "Третий абзац"], paras[-3:]

        # 3. XLSX: многострочный --text -> несколько строк
        xlsx = Path(tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False).name)
        Workbook().save(xlsx)
        try:
            proc = run_cli("insert-text", "--file", str(xlsx),
                           "--text", "строка 1\\nстрока 2", "--in-place")
            assert proc.returncode == 0 and "вставлено строк: 2" in proc.stdout, proc.stdout
            ws = load_workbook(xlsx).active
            assert ws["A1"].value == "строка 1" and ws["A2"].value == "строка 2"
        finally:
            xlsx.unlink(missing_ok=True)

        # 4. apply с ДОБАВЛЕННЫМИ строками указывает на insert-text
        proc = run_cli("dump", "--file", str(out), "--format", "text")
        dump_path = out.with_name("edit_check.txt")
        dump_path.write_text(proc.stdout + "\nДОБАВЛЕННАЯ СТРОКА\nЕЩЁ ОДНА\n",
                             encoding="utf-8")
        proc = run_cli("apply", "--file", str(out),
                       "--from-text", str(dump_path), "--output", str(out))
        assert proc.returncode != 0 and "insert-text" in proc.stderr, proc.stderr
        dump_path.unlink(missing_ok=True)
        print("TEST 15 OK: insert-text делит переносы на абзацы; apply -> insert-text")
    finally:
        for p in (docx, out, Path(str(docx) + ".tmp"), Path(str(out) + ".tmp")):
            p.unlink(missing_ok=True)


def test_docx_add_column_position() -> None:
    """add-column (DOCX): вставка столбца в середину таблицы справа/слева
    от столбца-ориентира (по номеру и по заголовку), gridCol добавляется,
    соседние столбцы сдвигаются; неоднозначные сочетания отклоняются."""
    docx = make_docx()  # таблица 3x3: Наименование | Кол-во | Цена
    out = docx.with_name(docx.stem + ".result.docx")
    try:
        # 1. Вставка СПРАВА от «Кол-во» (по заголовку) со значениями
        proc = run_cli("add-column", "--file", str(docx), "--table", "0",
                       "--column", "Кол-во", "--position", "after",
                       "--header", "Ед. изм.",
                       "--cell", "м", "--cell", "шт",
                       "--output", str(out))
        assert proc.returncode == 0, proc.stderr
        assert "вставлен на позицию 2" in proc.stdout, proc.stdout
        t = Document(out).tables[0]
        assert len(t.columns) == 4, len(t.columns)
        assert [c.text for c in t.rows[0].cells] == \
            ["Наименование", "Кол-во", "Ед. изм.", "Цена"]
        assert t.rows[1].cells[2].text == "м"
        assert t.rows[2].cells[2].text == "шт"
        # исходник не тронут
        assert len(Document(docx).tables[0].columns) == 3

        # 2. Вставка СЛЕВА от «Цена» (по номеру 2 в файле результата)
        proc = run_cli("add-column", "--file", str(out), "--table", "0",
                       "--column", "2", "--position", "before",
                       "--header", "Примечание", "--in-place")
        assert proc.returncode == 0 and "вставлен на позицию 2" in proc.stdout, proc.stdout
        t = Document(out).tables[0]
        assert [c.text for c in t.rows[0].cells] == \
            ["Наименование", "Кол-во", "Примечание", "Ед. изм.", "Цена"]

        # 3. --table all с --column — отказ (куда вставлять во всех таблицах?)
        proc = run_cli("add-column", "--file", str(out), "--table", "all",
                       "--column", "Цена", "--header", "X", "--in-place")
        assert proc.returncode != 0 and "--table all" in proc.stderr, proc.stderr

        # 4. Незнакомый заголовок — ошибка со списком заголовков
        proc = run_cli("add-column", "--file", str(out), "--table", "0",
                       "--column", "Нет такого", "--header", "X", "--in-place")
        assert proc.returncode != 0 and "не найден" in proc.stderr \
            and "Наименование" in proc.stderr, proc.stderr
        print("TEST 16 OK: add-column (DOCX) — вставка по ориентиру before/after")
    finally:
        for p in (docx, out, Path(str(docx) + ".tmp"), Path(str(out) + ".tmp")):
            p.unlink(missing_ok=True)


def test_read_delete_column_by_docx_header() -> None:
    """read-column/delete-column (DOCX): --column принимает не только
    номер, но и текст заголовка (регрессия «invalid literal for int()»)."""
    docx = make_docx()  # Наименование | Кол-во | Цена; строки: 10/100, 5/50
    out = docx.with_name(docx.stem + ".result.docx")
    try:
        # 1. Чтение по тексту заголовка
        proc = run_cli("read-column", "--file", str(docx), "--table", "0",
                       "--column", "Цена")
        assert proc.returncode == 0, proc.stderr
        assert "Таблица 0, колонка 2 (Цена)" in proc.stdout, proc.stdout
        assert "строка 1: 100" in proc.stdout, proc.stdout
        assert "строка 2: 50" in proc.stdout, proc.stdout

        # 2. Чтение по номеру по-прежнему работает
        proc = run_cli("read-column", "--file", str(docx), "--table", "0",
                       "--column", "1")
        assert "Таблица 0, колонка 1 (Кол-во)" in proc.stdout, proc.stdout

        # 3. Удаление по тексту заголовка
        proc = run_cli("delete-column", "--file", str(docx), "--table", "0",
                       "--column", "Цена", "--output", str(out))
        assert proc.returncode == 0, proc.stderr
        t = Document(out).tables[0]
        assert len(t.columns) == 2 and t.rows[0].cells[1].text == "Кол-во"
        # исходник не тронут
        assert len(Document(docx).tables[0].columns) == 3

        # 4. Незнакомый заголовок — ошибка с перечнем заголовков таблицы
        proc = run_cli("read-column", "--file", str(docx), "--table", "0",
                       "--column", "Нет такого")
        assert proc.returncode != 0 and "не найден" in proc.stderr \
            and "Наименование" in proc.stderr, proc.stderr
        print("TEST 17 OK: read-column/delete-column (DOCX) по заголовку")
    finally:
        for p in (docx, out, Path(str(docx) + ".tmp"), Path(str(out) + ".tmp")):
            p.unlink(missing_ok=True)


def test_insert_text() -> None:
    """insert-text: вставка новых абзацев (DOCX) по якорю и в конец,
    строк (XLSX) по номеру строки и в конец; ошибка при ненайденном якоре."""
    tmp = tempfile.NamedTemporaryFile(suffix=".docx", delete=False)
    tmp.close()
    docx_path = Path(tmp.name)
    xlsx_path = docx_path.with_suffix(".xlsx")
    result = docx_path.with_name(docx_path.stem + ".result.docx")
    try:
        doc = Document()
        doc.add_paragraph("Вводная часть договора подряда")
        doc.add_paragraph("Условия оплаты")
        doc.save(docx_path)

        # 1. Вставка ПОСЛЕ якоря: порядок абзацев правильный
        proc = run_cli("insert-text", "--file", str(docx_path),
                       "--anchor", "Условия оплаты", "--position", "after",
                       "--text", "Резюме: договор выгоден.",
                       "--text", "Итоговая рекомендация — положительная.",
                       "--output", str(result))
        assert proc.returncode == 0, proc.stderr
        out_doc = Document(result)
        texts = [p.text for p in out_doc.paragraphs]
        assert texts == ["Вводная часть договора подряда", "Условия оплаты",
                         "Резюме: договор выгоден.",
                         "Итоговая рекомендация — положительная."], texts

        # 2. Вставка ДО якоря (в файл результата, поверх)
        proc = run_cli("insert-text", "--file", str(result),
                       "--anchor", "Вводная часть", "--position", "before",
                       "--text", "Заголовок документа", "--in-place")
        assert proc.returncode == 0, proc.stderr
        texts = [p.text for p in Document(result).paragraphs]
        assert texts[0] == "Заголовок документа"
        assert texts[1] == "Вводная часть договора подряда"

        # 3. Без якоря — в конец документа
        proc = run_cli("insert-text", "--file", str(result),
                       "--text", "Финальная строка", "--in-place")
        assert proc.returncode == 0, proc.stderr
        texts = [p.text for p in Document(result).paragraphs]
        assert texts[-1] == "Финальная строка"

        # 4. Ненайденный якорь — явная ошибка, файл не изменён
        before = result.read_bytes()
        proc = run_cli("insert-text", "--file", str(result),
                       "--anchor", "нет такого абзаца",
                       "--text", "X", "--in-place")
        assert proc.returncode != 0
        assert "не найден" in proc.stderr
        assert result.read_bytes() == before

        # 5. XLSX: вставка перед строкой сдвигает существующие вниз
        wb = Workbook()
        ws = wb.active
        ws["A1"] = "шапка"
        ws["A2"] = "данные"
        wb.save(xlsx_path)
        xlsx_result = xlsx_path.with_name(xlsx_path.stem + ".result.xlsx")
        proc = run_cli("insert-text", "--file", str(xlsx_path),
                       "--row", "2", "--text", "строка-вставка",
                       "--output", str(xlsx_result))
        assert proc.returncode == 0, proc.stderr
        ws2 = load_workbook(xlsx_result).active
        assert (ws2["A1"].value, ws2["A2"].value,
                ws2["A3"].value) == ("шапка", "строка-вставка", "данные")

        # 6. XLSX: без --row — в конец листа
        proc = run_cli("insert-text", "--file", str(xlsx_result),
                       "--text", "итоговая строка", "--in-place")
        assert proc.returncode == 0, proc.stderr
        ws3 = load_workbook(xlsx_result).active
        assert ws3["A4"].value == "итоговая строка"
        print("TEST 11 OK: insert-text — вставка по якорю/в конец (DOCX и XLSX)")
    finally:
        for p in (docx_path, result, xlsx_path, xlsx_result,
                  Path(str(docx_path) + ".tmp"),
                  Path(str(xlsx_path) + ".tmp")):
            p.unlink(missing_ok=True)


def test_insert_text_occurrence() -> None:
    """insert-text (DOCX): --occurrence выбирает N-е вхождение якоря
    (по умолчанию — первое); --occurrence больше числа вхождений —
    явная ошибка, файл не изменён."""
    tmp = tempfile.NamedTemporaryFile(suffix=".docx", delete=False)
    tmp.close()
    docx_path = Path(tmp.name)
    result = docx_path.with_name(docx_path.stem + ".result.docx")
    try:
        doc = Document()
        doc.add_paragraph("Раздел: Итоги")
        doc.add_paragraph("Итоги первого этапа")
        doc.add_paragraph("Итоги второго этапа")
        doc.save(docx_path)

        # 1. Без --occurrence — первое вхождение (прежнее поведение)
        proc = run_cli("insert-text", "--file", str(docx_path),
                       "--anchor", "Итоги", "--position", "before",
                       "--text", "Перед первым", "--output", str(result))
        assert proc.returncode == 0, proc.stderr
        texts = [p.text for p in Document(result).paragraphs]
        assert texts[0] == "Перед первым", texts
        assert texts[1] == "Раздел: Итоги", texts

        # 2. --occurrence 2 после якоря — после «Итоги первого этапа»
        proc = run_cli("insert-text", "--file", str(result),
                       "--anchor", "Итоги", "--position", "after",
                       "--occurrence", "2",
                       "--text", "После второго", "--in-place")
        assert proc.returncode == 0, proc.stderr
        texts = [p.text for p in Document(result).paragraphs]
        assert texts == ["Перед первым", "Раздел: Итоги",
                         "Итоги первого этапа", "После второго",
                         "Итоги второго этапа"], texts

        # 3. --occurrence 3 перед якорем — перед «Итоги второго этапа»
        proc = run_cli("insert-text", "--file", str(result),
                       "--anchor", "Итоги", "--position", "before",
                       "--occurrence", "3",
                       "--text", "Перед третьим", "--in-place")
        assert proc.returncode == 0, proc.stderr
        texts = [p.text for p in Document(result).paragraphs]
        assert texts == ["Перед первым", "Раздел: Итоги",
                         "Итоги первого этапа", "После второго",
                         "Перед третьим", "Итоги второго этапа"], texts

        # 4. --occurrence больше числа вхождений — ошибка, файл не изменён
        before = result.read_bytes()
        proc = run_cli("insert-text", "--file", str(result),
                       "--anchor", "Итоги", "--occurrence", "10",
                       "--text", "X", "--in-place")
        assert proc.returncode != 0
        assert "встречается" in proc.stderr, proc.stderr
        assert result.read_bytes() == before
        print("TEST 18 OK: insert-text --occurrence — N-е вхождение якоря")
    finally:
        for p in (docx_path, result, Path(str(docx_path) + ".tmp"),
                  Path(str(result) + ".tmp")):
            p.unlink(missing_ok=True)


def test_dump_find_outfile() -> None:
    """dump: --find выводит только строки с подстрокой (регистр и ё/е
    не важны, промах — явная ошибка); --out-file пишет валидный UTF-8."""
    tmp = tempfile.NamedTemporaryFile(suffix=".docx", delete=False)
    tmp.close()
    docx_path = Path(tmp.name)
    out_md = docx_path.with_name("dump_test_out.md")
    try:
        doc = Document()
        doc.add_paragraph("Вводная часть")
        doc.add_paragraph("Порядок оплаты")
        doc.add_paragraph("Автоматический Учёт сумм")
        doc.add_paragraph("Порядок оплаты работ")
        doc.save(docx_path)

        # 1. --find без учёта регистра
        proc = run_cli("dump", "--file", str(docx_path),
                       "--find", "порядок оплаты")
        assert proc.returncode == 0, proc.stderr
        assert "Порядок оплаты" in proc.stdout, proc.stdout
        assert "Порядок оплаты работ" in proc.stdout, proc.stdout
        assert "Вводная часть" not in proc.stdout, proc.stdout

        # 2. --find: ё в запросе покрывает е в тексте
        proc = run_cli("dump", "--file", str(docx_path), "--find", "учет")
        assert proc.returncode == 0, proc.stderr
        assert "Учёт сумм" in proc.stdout, proc.stdout

        # 3. Промах — явная ошибка, а не пустой вывод
        proc = run_cli("dump", "--file", str(docx_path),
                       "--find", "нет такой строки")
        assert proc.returncode != 0
        assert "не найдена" in proc.stderr, proc.stderr

        # 4. --out-file: валидный UTF-8 (не UTF-16LE), фильтр применён
        proc = run_cli("dump", "--file", str(docx_path),
                       "--find", "Порядок", "--out-file", str(out_md))
        assert proc.returncode == 0, proc.stderr
        raw = out_md.read_bytes()
        assert not raw.startswith(b"\xff\xfe"), "UTF-16LE вместо UTF-8"
        content = out_md.read_text(encoding="utf-8")
        assert "Порядок оплаты" in content, content
        assert "Вводная часть" not in content, content
        print("TEST 19 OK: dump --find/--out-file — поиск и UTF-8-вывод")
    finally:
        for p in (docx_path, out_md, Path(str(docx_path) + ".tmp")):
            p.unlink(missing_ok=True)


def main():
    test_list_tables_and_dump()
    test_replace_text_preserves_original()
    test_set_cell_add_row_add_column()
    test_apply_roundtrip()
    test_xlsx_set_value_append_row()
    test_office_ops_hint_injection()
    test_add_column_all_tables()
    test_office_ops_hint_client_agnostic()
    test_write_guard_anonymized_copy()
    test_office_edit_chain_hint()
    test_insert_text()
    test_insert_text_occurrence()
    test_dump_find_outfile()
    test_read_column_and_delete_column()
    test_xlsx_add_column()
    test_guard_result_naming_and_dates()
    test_insert_text_multiline()
    test_docx_add_column_position()
    test_read_delete_column_by_docx_header()
    print("\nALL OFFICE OPS TESTS PASSED")


if __name__ == "__main__":
    main()