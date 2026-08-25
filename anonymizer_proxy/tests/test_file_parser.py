"""
Функциональные тесты FileParser/FileAssembler: сохранение структуры
таблиц DOCX и XLSX при извлечении текста и обратной сборке.

Проверяют:
1. DOCX: абзацы и ячейки таблиц извлекаются, при сборке сетка таблицы
   сохраняется, заменяется только изменённый текст.
2. DOCX: ячейка из нескольких абзацев не даёт смещения при сборке.
3. XLSX: числа, даты и формулы сохраняются, заменяется только ячейка с PII.
4. XLSX: пустые ячейки не дают лишних строк и не ломают выравнивание.
5. DOCX: колонтитулы и текстовые фигуры извлекаются и анонимизируются;
   картинки из колонтитулов удаляются вместе с media-файлами пакета;
   де-анонимизация восстанавливает исходный текст.

Запуск: python anonymizer_proxy\\\\tests\\\\test_file_parser.py (из корня проекта)
"""
import asyncio
import base64
import io
import sys
import zipfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from docx import Document
from docx.oxml import parse_xml
from docx.oxml.ns import qn
from docx.shared import Inches
from docx.text.paragraph import Paragraph
from openpyxl import Workbook, load_workbook

from anonymizer_proxy.anonymizer.file_parser import (
    FileParser,
    FileAssembler,
    _txbx_para_elements,
)


def make_docx(make) -> bytes:
    doc = Document()
    make(doc)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# Текстовая фигура как в реальных документах Word: mc:AlternateContent с
# ветками Choice (DrawingML) и Fallback (VML), текст продублирован
_TEXTBOX_PII = "Получатель: Милюков Анатолий Анатольевич"

_TXBX_XML = (
    '<w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    '<w:r>'
    '<mc:AlternateContent'
    ' xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"'
    ' xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape">'
    '<mc:Choice Requires="wps">'
    '<w:drawing>'
    '<wp:anchor'
    ' xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"'
    ' distT="0" distB="0" distL="0" distR="0" simplePos="0" relativeHeight="1"'
    ' behindDoc="0" locked="0" layoutInCell="1" allowOverlap="1">'
    '<wp:simplePos x="0" y="0"/>'
    '<wp:positionH relativeFrom="column"><wp:posOffset>0</wp:posOffset></wp:positionH>'
    '<wp:positionV relativeFrom="paragraph"><wp:posOffset>0</wp:posOffset></wp:positionV>'
    '<wp:extent cx="285750" cy="285750"/>'
    '<a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
    '<a:graphicData uri="http://schemas.microsoft.com/office/word/2010/wordprocessingShape">'
    '<wps:wsp>'
    '<wps:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="285750" cy="285750"/></a:xfrm>'
    '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></wps:spPr>'
    '<wps:txbx><w:txbxContent>'
    '<w:p><w:r><w:t>' + _TEXTBOX_PII + '</w:t></w:r></w:p>'
    '</w:txbxContent></wps:txbx>'
    '</wps:wsp></a:graphicData></a:graphic></wp:anchor></w:drawing>'
    '</mc:Choice>'
    '<mc:Fallback><w:pict>'
    '<v:shape xmlns:v="urn:schemas-microsoft-com:vml"'
    ' style="width:22.5pt;height:22.5pt">'
    '<v:textbox><w:txbxContent>'
    '<w:p><w:r><w:t>' + _TEXTBOX_PII + '</w:t></w:r></w:p>'
    '</w:txbxContent></v:textbox></v:shape>'
    '</w:pict></mc:Fallback>'
    '</mc:AlternateContent></w:r></w:p>'
)

# 1x1 PNG (пиксель-картинка для проверки удаления логотипов)
_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUl"
    "EQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


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


async def test_docx_headers_footers_textboxes_images():
    """DOCX: колонтитулы/фигуры анонимизируются, картинки удаляются,
    де-анонимизация восстанавливает текст (round-trip)"""

    def make(doc):
        doc.add_paragraph("Тело документа — Иван Петров")
        sec = doc.sections[0]
        sec.header.is_linked_to_previous = False
        sec.header.paragraphs[0].text = "ООО «Ромашка»: Иванов Иван"
        # Таблица внутри колонтитула
        tbl = sec.header.add_table(rows=1, cols=1, width=Inches(3))
        tbl.cell(0, 0).text = "Бухгалтер Иван Петров"
        # Подвал: PII + картинка-«логотип»
        sec.footer.paragraphs[0].text = "Контакты: Иван Петров"
        pic_para = sec.footer.add_paragraph()
        pic_para.add_run().add_picture(
            io.BytesIO(base64.b64decode(_TINY_PNG_B64)), width=Inches(1))
        # Текстовая фигура в теле документа
        doc.element.body.append(parse_xml(_TXBX_XML))

    content = make_docx(make)
    fp, fa = FileParser(), FileAssembler()
    parsed = await fp.parse(content, "t.docx")
    lines = parsed.text.split("\n")

    # Извлечение: тело + колонтитулы (абзацы и таблицы) + фигуры
    assert "Тело документа — Иван Петров" in lines
    assert "ООО «Ромашка»: Иванов Иван" in lines
    assert "Бухгалтер Иван Петров" in lines
    assert "Контакты: Иван Петров" in lines
    # Фигура продублирована в mc:Choice/mc:Fallback -> два сегмента
    assert lines.count(_TEXTBOX_PII) == 2, lines
    kinds = {seg["kind"] for seg in parsed.structure["segments"]}
    assert {"hf_para", "hf_cell_para", "txbx_para"} <= kinds, kinds

    # Имитируем анонимизацию
    anon = (parsed.text
            .replace("Ромашка", "[ORG_1]")
            .replace("Иванов Иван", "[PERSON_3]")
            .replace("Иван Петров", "[PERSON_1]")
            .replace("Милюков Анатолий Анатольевич", "[PERSON_2]"))
    out = await fa.assemble(content, "t.docx", anon, parsed.structure)
    return fa, content, parsed, anon, out


async def check_docx_headers_footers_result(fa, content, parsed, anon, out):
    """Проверки результата сборки + round-trip де-анонимизации"""
    doc2 = Document(io.BytesIO(out))
    hdr = doc2.sections[0].header
    ftr = doc2.sections[0].footer
    assert hdr.paragraphs[0].text == "ООО «[ORG_1]»: [PERSON_3]"
    assert hdr.tables[0].cell(0, 0).text == "Бухгалтер [PERSON_1]"
    assert ftr.paragraphs[0].text == "Контакты: [PERSON_1]"

    # Картинки из колонтитулов удалены (и DrawingML, и VML)
    assert ftr.part.element.find(".//" + qn("w:drawing")) is None
    assert ftr.part.element.find(".//" + qn("w:pict")) is None

    # Фигура заменена в обеих ветках (Choice и Fallback)
    shape_texts = [
        Paragraph(p_el, None).text
        for box in _txbx_para_elements(doc2.element)
        for p_el in box
    ]
    assert shape_texts.count("Получатель: [PERSON_2]") == 2, shape_texts
    assert all("Милюков" not in t for t in shape_texts), shape_texts

    # Media-файлы (байты логотипов) вымыты из пакета
    with zipfile.ZipFile(io.BytesIO(out)) as z:
        media = [n for n in z.namelist() if n.startswith("word/media/")]
    assert not media, media

    # Round-trip: де-анонимизация восстанавливает исходный текст
    restored = (anon
                .replace("[ORG_1]", "Ромашка")
                .replace("[PERSON_3]", "Иванов Иван")
                .replace("[PERSON_2]", "Милюков Анатолий Анатольевич")
                .replace("[PERSON_1]", "Иван Петров"))
    out2 = await fa.assemble(content, "t.docx", restored, parsed.structure)
    doc3 = Document(io.BytesIO(out2))
    sec3 = doc3.sections[0]
    assert sec3.header.paragraphs[0].text == "ООО «Ромашка»: Иванов Иван"
    assert sec3.header.tables[0].cell(0, 0).text == "Бухгалтер Иван Петров"
    assert sec3.footer.paragraphs[0].text == "Контакты: Иван Петров"
    rt_shapes = [
        Paragraph(p_el, None).text
        for box in _txbx_para_elements(doc3.element)
        for p_el in box
    ]
    assert rt_shapes.count(_TEXTBOX_PII) == 2, rt_shapes
    print("TEST 5 OK: DOCX — колонтитулы/фигуры анонимизируются, "
          "логотипы удалены, round-trip работает")


async def main():
    await test_docx_table_structure()
    await test_docx_multiparagraph_cell()
    await test_xlsx_types_and_formulas()
    await test_xlsx_empty_cells()
    fa2, content, parsed2, anon, out = (
        await test_docx_headers_footers_textboxes_images())
    await check_docx_headers_footers_result(fa2, content, parsed2, anon, out)
    print("\nALL FILE PARSER TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
