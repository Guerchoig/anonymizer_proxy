"""
Функциональные тесты FileParser/FileAssembler: сохранение структуры
таблиц DOCX и XLSX при извлечении текста и обратной сборке.

Проверяют:
1. DOCX: абзацы и ячейки таблиц извлекаются, при сборке сетка таблицы
   сохраняется, заменяется только изменённый текст.
2. DOCX: ячейка из нескольких абзацев не даёт смещения при сборке.
3. XLSX: числа, даты и формулы сохраняются, заменяется только ячейка с PII.
4. XLSX: пустые ячейки не дают лишних строк и не ломают выравнивание.

Запуск: python anonymizer_proxy\\tests\\test_file_parser.py (из корня проекта)
"""
import asyncio
import io
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from docx import Document
from openpyxl import Workbook, load_workbook

from anonymizer_proxy.anonymizer.file_parser import FileParser, FileAssembler


def make_docx(make) -> bytes:
    doc = Document()
    make(doc)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


async def test_docx_table_structure():
    """DOCX: структура таблицы сохраняется, замена точечная"""
    def make(doc):
        doc.add_paragraph("Шапка документа")
        table = doc.add_table(rows=2, cols=2)
        table.cell(0, 0).text = "Сотрудник"
        table.cell(0, 1).text = "Должность"
        table.cell(1, 0).text = "Иван Петров"
        table.cell(1, 1).text = "Бухгалтер"
        doc.add_paragraph("Подвал документа")

    content = make_docx(make)
    fp, fa = FileParser(), FileAssembler()
    parsed = await fp.parse(content, "t.docx")

    # Извлечённый текст содержит и абзацы, и ячейки таблицы
    lines = parsed.text.split("\n")
    for expected in ["Шапка документа", "Сотрудник", "Иван Петров",
                     "Подвал документа"]:
        assert expected in lines, parsed.text

    # Имитируем анонимизацию: замена PII в тексте
    anon = parsed.text.replace("Иван Петров", "[PERSON_1]")
    out = await fa.assemble(content, "t.docx", anon, parsed.structure)

    doc2 = Document(io.BytesIO(out))
    assert doc2.paragraphs[0].text == "Шапка документа"
    assert doc2.paragraphs[-1].text == "Подвал документа"
    assert len(doc2.tables) == 1
    t = doc2.tables[0]
    assert len(t.rows) == 2 and len(t.columns) == 2
    assert t.cell(0, 0).text == "Сотрудник"
    assert t.cell(0, 1).text == "Должность"
    assert t.cell(1, 0).text == "[PERSON_1]"
    assert t.cell(1, 1).text == "Бухгалтер"
    print("TEST 1 OK: DOCX — структура таблицы сохранена, замена точечная")


async def test_docx_multiparagraph_cell():
    """DOCX: ячейка из нескольких абзацев не даёт смещения при сборке"""
    def make(doc):
        table = doc.add_table(rows=1, cols=2)
        cell0 = table.cell(0, 0)
        cell0.text = "Первый абзац ячейки"
        cell0.add_paragraph("Второй абзац — Иван Петров")
        table.cell(0, 1).text = "Соседняя ячейка"

    content = make_docx(make)
    fp, fa = FileParser(), FileAssembler()
    parsed = await fp.parse(content, "t.docx")

    anon = parsed.text.replace("Иван Петров", "[PERSON_1]")
    out = await fa.assemble(content, "t.docx", anon, parsed.structure)

    doc2 = Document(io.BytesIO(out))
    c = doc2.tables[0].cell(0, 0)
    assert c.paragraphs[0].text == "Первый абзац ячейки", c.paragraphs[0].text
    assert c.paragraphs[1].text == "Второй абзац — [PERSON_1]", \
        c.paragraphs[1].text
    assert doc2.tables[0].cell(0, 1).text == "Соседняя ячейка"
    print("TEST 2 OK: DOCX — многострочная ячейка без смещения")


async def test_xlsx_types_and_formulas():
    """XLSX: числа/даты/формулы сохраняются, заменяется только ячейка с PII"""
    wb = Workbook()
    ws = wb.active
    ws.title = "Данные"
    ws["A1"], ws["B1"], ws["C1"], ws["D1"] = (
        "Сотрудник", "Оклад", "Дата приёма", "Премия"
    )
    ws["A2"] = "Иван Петров"
    ws["B2"] = 50000
    ws["C2"] = datetime(2023, 4, 10)
    ws["D2"] = "=B2*0.1"
    buf = io.BytesIO()
    wb.save(buf)
    content = buf.getvalue()

    fp, fa = FileParser(), FileAssembler()
    parsed = await fp.parse(content, "t.xlsx")
    lines = parsed.text.split("\n")
    assert "Иван Петров" in lines, lines
    assert "50000" in lines, lines
    assert "=B2*0.1" in lines, lines

    anon = parsed.text.replace("Иван Петров", "[PERSON_1]")
    out = await fa.assemble(content, "t.xlsx", anon, parsed.structure)

    wb2 = load_workbook(io.BytesIO(out))
    ws2 = wb2["Данные"]
    assert ws2["A1"].value == "Сотрудник"
    assert ws2["A2"].value == "[PERSON_1]"
    assert ws2["B2"].value == 50000 and isinstance(ws2["B2"].value, (int, float))
    assert ws2["C2"].value == datetime(2023, 4, 10)
    assert ws2["D2"].value == "=B2*0.1", ws2["D2"].value  # формула сохранена
    print("TEST 3 OK: XLSX — числа/даты/формулы сохранены, PII заменено")


async def test_xlsx_empty_cells():
    """XLSX: пустые ячейки не дают лишних строк и не ломают выравнивание"""
    wb = Workbook()
    ws = wb.active
    ws["A1"], ws["B1"], ws["C1"] = "Имя", None, "Фамилия"
    ws["A2"], ws["B2"], ws["C2"] = "Иван", "", "Петров"
    buf = io.BytesIO()
    wb.save(buf)
    content = buf.getvalue()

    fp, fa = FileParser(), FileAssembler()
    parsed = await fp.parse(content, "t.xlsx")
    assert parsed.text.split("\n") == ["Имя", "Фамилия", "Иван", "Петров"], \
        parsed.text

    anon = parsed.text.replace("Петров", "[PERSON_1]")
    out = await fa.assemble(content, "t.xlsx", anon, parsed.structure)

    wb2 = load_workbook(io.BytesIO(out))
    ws2 = wb2.active
    assert ws2["A1"].value == "Имя"
    assert ws2["C1"].value == "Фамилия"
    assert ws2["A2"].value == "Иван"
    assert ws2["C2"].value == "[PERSON_1]"
    print("TEST 4 OK: XLSX — пустые ячейки не ломают выравнивание")


async def main():
    await test_docx_table_structure()
    await test_docx_multiparagraph_cell()
    await test_xlsx_types_and_formulas()
    await test_xlsx_empty_cells()
    print("\nALL FILE PARSER TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
