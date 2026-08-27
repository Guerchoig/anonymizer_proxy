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


def main():
    test_list_tables_and_dump()
    test_replace_text_preserves_original()
    test_set_cell_add_row_add_column()
    test_apply_roundtrip()
    test_xlsx_set_value_append_row()
    test_office_ops_hint_injection()
    test_add_column_all_tables()
    print("\nALL OFFICE OPS TESTS PASSED")


if __name__ == "__main__":
    main()