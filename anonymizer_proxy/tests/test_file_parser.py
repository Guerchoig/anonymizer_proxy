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
import xml.etree.ElementTree as ET
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
from anonymizer_proxy.anonymizer.ner_service import NERService


def make_docx(make) -> bytes:
    doc = Document()
    make(doc)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# Content types / rel types для инъекции частей в тестовый DOCX-пакет
_CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CT_COMMENTS = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml")
_RT_COMMENTS = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments")
_CT_PEOPLE = "application/vnd.microsoft.office.word.people+xml"
_RT_PEOPLE = "http://schemas.microsoft.com/office/2011/relationships/people"

_COMMENTS_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    '<w:comment w:id="1" w:author="Sasha" w:initials="S"'
    ' w:date="2026-09-08T12:00:00Z">'
    '<w:p><w:r><w:t>Согласовано: А.В. Гершойг</w:t></w:r></w:p>'
    '</w:comment></w:comments>'
)
_PEOPLE_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<w15:people xmlns:w15="http://schemas.microsoft.com/office/word/2012/wordml">'
    '<w15:person w15:author="Sasha">'
    '<w15:presenceInfo w15:providerId="None" w15:userId="Sasha"/>'
    '</w15:person></w15:people>'
)


def _inject_parts(content: bytes, entries: dict) -> bytes:
    """Добавить в DOCX-пакет дополнительные части (comments.xml, people.xml):
    сами файлы, Override в [Content_Types].xml и Relationship в
    word/_rels/document.xml.rels (python-docx 1.2 не умеет создавать
    комментарии — читает их, поэтому собираем пакет вручную)."""
    with zipfile.ZipFile(io.BytesIO(content)) as zin:
        infos = zin.infolist()
        data = {i.filename: zin.read(i.filename) for i in infos}

    ct = ET.fromstring(data["[Content_Types].xml"])
    rels = ET.fromstring(data["word/_rels/document.xml.rels"])
    rid_n = 900
    for name, (blob, ctype, rel_type) in entries.items():
        data[name] = blob
        override = ET.SubElement(ct, f"{{{_CT_NS}}}Override")
        override.set("PartName", "/" + name)
        override.set("ContentType", ctype)
        rid_n += 1
        rel = ET.SubElement(rels, f"{{{_RELS_NS}}}Relationship")
        rel.set("Id", f"rIdInject{rid_n}")
        rel.set("Type", rel_type)
        rel.set("Target", name[len("word/"):] if name.startswith("word/")
                else name)

    ET.register_namespace("", _CT_NS)
    data["[Content_Types].xml"] = ET.tostring(
        ct, xml_declaration=True, encoding="UTF-8")
    ET.register_namespace("", _RELS_NS)
    data["word/_rels/document.xml.rels"] = ET.tostring(
        rels, xml_declaration=True, encoding="UTF-8")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
        written = set()
        for i in infos:
            zout.writestr(i, data[i.filename])
            written.add(i.filename)
        # новые части (комментарии, people) — их нет в исходном infolist
        for name, blob in data.items():
            if name not in written:
                zout.writestr(name, blob)
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


async def test_hyperlink_targets_scrubbed():
    """Анонимизирующая сборка вычищает внешние цели гиперссылок из .rels
    (адрес сайта не утечёт из пакета); де-анонимизирующая не трогает пакет"""
    import xml.etree.ElementTree as ET

    url = "https://www.iris-retail.ru/tenders"
    site = "www.iris-retail.ru"

    def make(doc):
        doc.add_paragraph(f"Контактный сайт: {site}")
        doc.add_paragraph("Почта для заявок: info@iris-retail.ru")

    content = make_docx(make)

    # Вживляем внешнюю гиперссылку в rels (как её хранит Word)
    with zipfile.ZipFile(io.BytesIO(content)) as zin:
        infos = zin.infolist()
        items = {i.filename: zin.read(i.filename) for i in infos}
    rels_name = "word/_rels/document.xml.rels"
    ns = "{http://schemas.openxmlformats.org/package/2006/relationships}"
    root = ET.fromstring(items[rels_name])
    rel = ET.SubElement(root, ns + "Relationship")
    rel.set("Id", "rIdWeb999")
    rel.set("Type", "http://schemas.openxmlformats.org/officeDocument"
                    "/2006/relationships/hyperlink")
    rel.set("Target", url)
    rel.set("TargetMode", "External")
    items[rels_name] = ET.tostring(
        root, xml_declaration=True, encoding="UTF-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for info in infos:
            z.writestr(info, items[info.filename])
    content = buf.getvalue()

    parser = FileParser()
    fa = FileAssembler()
    parsed = await parser.parse(content, "t.docx")
    anon = parsed.text.replace(site, "[WEB_1]")

    # Анонимизирующая сборка: цель гиперссылки нейтрализуется
    out = await fa.assemble(content, "t.docx", anon, parsed.structure,
                            scrub_metadata=True)
    with zipfile.ZipFile(io.BytesIO(out)) as z:
        rels_after = z.read(rels_name).decode("utf-8")
    assert url not in rels_after, "URL гиперссылки остался в rels!"
    assert 'Target="#"' in rels_after, rels_after
    assert site not in rels_after
    Document(io.BytesIO(out))  # пакет остаётся валидным DOCX

    # Де-анонимизирующая сборка: пакет не трогается
    out2 = await fa.assemble(content, "t.docx", anon, parsed.structure,
                             scrub_metadata=False)
    with zipfile.ZipFile(io.BytesIO(out2)) as z:
        rels_kept = z.read(rels_name).decode("utf-8")
    assert url in rels_kept, "де-анонимизация не должна трогать rels"
    print("TEST 6 OK: гиперссылки — цели вычищаются при анонимизации, "
          "сохраняются при де-анонимизации")


async def test_page_number_fields():
    """Футер с полями PAGE/NUMPAGES: в изменённом абзаце поля удаляются
    (Word иначе дорисовывает номера страниц поверх маскировки —
    «Страница 2 из 12» превращается в «[LOC_1]212»), в неизменённом
    и восстановленном — сохраняются"""
    import re
    from docx.oxml import OxmlElement

    def add_field(paragraph, instr: str, cached: str) -> None:
        r1 = paragraph.add_run()
        fld = OxmlElement("w:fldChar")
        fld.set(qn("w:fldCharType"), "begin")
        r1._r.append(fld)
        r2 = paragraph.add_run()
        it = OxmlElement("w:instrText")
        it.set(qn("xml:space"), "preserve")
        it.text = f" {instr}  \\* Arabic "
        r2._r.append(it)
        r3 = paragraph.add_run()
        sep = OxmlElement("w:fldChar")
        sep.set(qn("w:fldCharType"), "separate")
        r3._r.append(sep)
        paragraph.add_run(cached)  # кэш результата поля
        r5 = paragraph.add_run()
        end = OxmlElement("w:fldChar")
        end.set(qn("w:fldCharType"), "end")
        r5._r.append(end)

    def make(doc):
        doc.add_paragraph("Договор с ООО «Ромашка»")
        para = doc.sections[0].footer.paragraphs[0]
        para.add_run("Страница ")
        add_field(para, "PAGE", "2")
        para.add_run(" из ")
        add_field(para, "NUMPAGES", "12")

    content = make_docx(make)
    parser = FileParser()
    fa = FileAssembler()

    def footer_xml(docx_bytes: bytes) -> str:
        with zipfile.ZipFile(io.BytesIO(docx_bytes)) as z:
            for n in z.namelist():
                if re.match(r"word/footer\d+\.xml$", n):
                    return z.read(n).decode("utf-8")
        return ""

    parsed = await parser.parse(content, "t.docx")
    # Кэш результатов полей входит в извлечённый текст сегмента
    assert "Страница 2 из 12" in parsed.text, parsed.text

    # 1) Неизменённый футер — структура полей сохранена
    out1 = await fa.assemble(content, "t.docx", parsed.text, parsed.structure)
    xml1 = footer_xml(out1)
    assert xml1.count("fldChar") >= 6, "поля исчезли в неизменённом футере"
    assert "instrText" in xml1

    # 2) Маскировка: текст абзаца точен, «призрачных» полей нет
    anon = parsed.text.replace("Страница 2 из 12", "[LOC_1]")
    out2 = await fa.assemble(content, "t.docx", anon, parsed.structure)
    doc2 = Document(io.BytesIO(out2))
    ftext = doc2.sections[0].footer.paragraphs[0].text
    assert ftext == "[LOC_1]", f"рендер футера: {ftext!r}"
    xml2 = footer_xml(out2)
    assert "fldChar" not in xml2 and "instrText" not in xml2, \
        "структура полей осталась в изменённом футере — Word дорисует номера"

    # 3) Де-анонимизация возвращает исходный текст — поля оживают
    out3 = await fa.assemble(content, "t.docx", parsed.text, parsed.structure)
    assert footer_xml(out3).count("fldChar") >= 6
    print("TEST 7 OK: поля PAGE/NUMPAGES — в изменённом футере удалены, "
          "в неизменённом/восстановленном сохранены")


async def test_docx_nested_tables_and_comments():
    """Регрессия 2026-09-08: вложенные таблицы («УТВЕРЖДАЮ» на титульном
    листе) и комментарии (текст + автор) анонимизируются и восстанавливаются
    де-анонимизацией; имя автора вычищается и из word/people.xml."""
    parser, fa = FileParser(), FileAssembler()

    def make(doc):
        doc.add_paragraph("Шапка документа")
        table = doc.add_table(rows=1, cols=1)
        cell = table.rows[0].cells[0]
        cell.paragraphs[0].text = "Обёртка без PII"
        nested = cell.add_table(rows=2, cols=1)
        nested.rows[0].cells[0].paragraphs[0].text = (
            "Заместитель генерального директора "
            "ООО «Газпром нефтехим Салават»")
        nested.rows[1].cells[0].paragraphs[0].text = "А.З. Ахметшин"
        doc.add_paragraph("Подвал документа")

    content = _inject_parts(
        make_docx(make),
        {
            "word/comments.xml": (_COMMENTS_XML, _CT_COMMENTS, _RT_COMMENTS),
            "word/people.xml": (_PEOPLE_XML, _CT_PEOPLE, _RT_PEOPLE),
        },
    )

    parsed = await parser.parse(content, "t.docx")
    kinds = {s["kind"] for s in parsed.structure["segments"]}
    assert "nested_cell_para" in kinds, kinds
    assert "comment_author" in kinds and "comment_para" in kinds, kinds
    for needle in ("Газпром нефтехим Салават", "Ахметшин", "Гершойг", "Sasha"):
        assert needle in parsed.text, needle

    anon = (parsed.text
            .replace("Газпром нефтехим Салават", "[ORG_1]")
            .replace("Ахметшин", "[PERSON_1]")
            .replace("Гершойг", "[PERSON_2]")
            .replace("Sasha", "[PERSON_3]"))
    out = await fa.assemble(content, "t.docx", anon, parsed.structure)

    z = zipfile.ZipFile(io.BytesIO(out))
    doc_xml = z.read("word/document.xml").decode("utf-8")
    assert "Ахметшин" not in doc_xml, "PII осталась во вложенной таблице"
    assert "Газпром нефтехим Салават" not in doc_xml
    com_xml = z.read("word/comments.xml").decode("utf-8")
    assert "Гершойг" not in com_xml, "PII осталась в тексте комментария"
    assert "Sasha" not in com_xml, "автор комментария не обезличен"
    assert "[PERSON_3]" in com_xml and "[PERSON_2]" in com_xml
    ppl = z.read("word/people.xml").decode("utf-8")
    assert "Sasha" not in ppl, "имя автора утекло через people.xml"
    assert "[PERSON_3]" in ppl

    # де-анонимизация: токены -> значения
    parsed2 = await parser.parse(out, "t.docx")
    restored = (parsed2.text
                .replace("[ORG_1]", "Газпром нефтехим Салават")
                .replace("[PERSON_1]", "Ахметшин")
                .replace("[PERSON_2]", "Гершойг")
                .replace("[PERSON_3]", "Sasha"))
    out2 = await fa.assemble(
        out, "t.docx", restored, parsed2.structure,
        strip_hf_images=False, scrub_metadata=False)
    parsed3 = await parser.parse(out2, "t.docx")
    for needle in ("Газпром нефтехим Салават", "Ахметшин", "Гершойг", "Sasha"):
        assert needle in parsed3.text, f"не восстановлено: {needle}"
    com2 = zipfile.ZipFile(io.BytesIO(out2)).read(
        "word/comments.xml").decode("utf-8")
    assert "Гершойг" in com2 and "Sasha" in com2
    print("TEST 8 OK: DOCX — вложенные таблицы и комментарии (текст, автор, "
          "people.xml) анонимизируются и восстанавливаются")


async def test_xlsx_comments_roundtrip():
    """Регрессия 2026-09-08: комментарии ячеек XLSX (текст + автор)
    анонимизируются и восстанавливаются де-анонимизацией."""
    from openpyxl.comments import Comment

    parser, fa = FileParser(), FileAssembler()
    wb = Workbook()
    ws = wb.active
    ws["A1"] = "Согласование"
    ws["B2"] = 42
    ws["A1"].comment = Comment(
        "Согласовал: А.В. Гершойг\nЗамечаний нет", "Sasha")
    buf = io.BytesIO()
    wb.save(buf)
    content = buf.getvalue()

    parsed = await parser.parse(content, "t.xlsx")
    kinds = {s["kind"] for s in parsed.structure["segments"]}
    assert "xlsx_comment" in kinds and "xlsx_comment_author" in kinds, kinds
    assert "Гершойг" in parsed.text and "Sasha" in parsed.text

    anon = (parsed.text
            .replace("Гершойг", "[PERSON_2]")
            .replace("Sasha", "[PERSON_3]"))
    out = await fa.assemble(content, "t.xlsx", anon, parsed.structure)

    wb2 = load_workbook(io.BytesIO(out))
    ws2 = wb2.active
    c = ws2["A1"].comment
    assert c is not None, "комментарий потерян при сборке"
    assert "Гершойг" not in c.text and "[PERSON_2]" in c.text, c.text
    assert c.author == "[PERSON_3]", c.author
    assert ws2["B2"].value == 42, "чужая ячейка изменена"

    parsed2 = await parser.parse(out, "t.xlsx")
    restored = (parsed2.text
                .replace("[PERSON_2]", "Гершойг")
                .replace("[PERSON_3]", "Sasha"))
    out2 = await fa.assemble(out, "t.xlsx", restored, parsed2.structure)
    c3 = load_workbook(io.BytesIO(out2)).active["A1"].comment
    assert "Гершойг" in c3.text and c3.author == "Sasha", (c3.text, c3.author)
    print("TEST 9 OK: XLSX — комментарии ячеек (текст, автор) анонимизируются "
          "и восстанавливаются")


async def test_xlsx_numeric_roundtrip():
    """Регрессия 2026-09-10: числовая ячейка после анонимизации и
    де-анонимизации должна остаться ЧИСЛОМ (а не строкой «386887.422»
    с точкой). Анонимизация — полная замена значения ячейки токеном."""
    buf = io.BytesIO()
    wb = Workbook()
    ws = wb.active
    ws["A1"] = "Сумма"
    ws["B1"] = 386887.422
    ws["B2"] = 51903
    ws["C1"] = "Иванов"
    wb.save(buf)
    data = buf.getvalue()

    parser = FileParser()
    assembler = FileAssembler()
    parsed = await parser.parse(data, "t.xlsx")
    lines = parsed.text.split("\n")
    anon_lines = [
        "[MONEY_1]" if ln == "386887.422" else ln for ln in lines]
    anon = await assembler.assemble(
        data, "t.xlsx", "\n".join(anon_lines), parsed.structure)
    wb_anon = load_workbook(io.BytesIO(anon))
    assert wb_anon.active["B1"].value == "[MONEY_1]"
    assert wb_anon.active["B2"].value == 51903  # не тронута

    # де-анонимизация: токен -> исходное значение
    final_lines = [
        "386887.422" if ln == "[MONEY_1]" else ln for ln in anon_lines]
    final = await assembler.assemble(
        data, "t.xlsx", "\n".join(final_lines), parsed.structure)
    wb_final = load_workbook(io.BytesIO(final))
    v = wb_final.active["B1"].value
    assert isinstance(v, (int, float)), \
        f"число стало {type(v).__name__}: {v!r}"
    assert abs(v - 386887.422) < 1e-9, v
    assert wb_final.active["B2"].value == 51903
    assert wb_final.active["C1"].value == "Иванов"
    print("TEST OK: xlsx numeric round-trip — число не превращается в строку")


async def test_docx_money_roundtrip():
    """Регрессия 2026-09-10 (аудит DOCX): суммы с запятой в абзацах и
    ячейках таблиц DOCX не дробятся, восстанавливаются в исходной форме
    (в DOCX все значения — текст, потерь числового типа нет по определению)."""
    doc = Document()
    doc.add_paragraph("Итого по договору: 386 887,422 руб.")
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Сумма"
    table.cell(1, 0).text = "7 432 480,00"
    table.cell(1, 1).text = "руб."
    buf = io.BytesIO()
    doc.save(buf)
    data = buf.getvalue()

    parser = FileParser()
    assembler = FileAssembler()
    parsed = await parser.parse(data, "t.docx")
    # MONEY-значения присутствуют в тексте целиком (regex \d+ для дробной части)
    assert "386 887,422" in parsed.text, parsed.text
    assert "7 432 480,00" in parsed.text

    lines = parsed.text.split("\n")
    anon_lines = [
        ln.replace("386 887,422", "[MONEY_1]")
        .replace("7 432 480,00", "[MONEY_2]")
        for ln in lines
    ]
    anon = await assembler.assemble(
        data, "t.docx", "\n".join(anon_lines), parsed.structure)
    doc_anon = Document(io.BytesIO(anon))
    assert "[MONEY_1]" in doc_anon.paragraphs[0].text
    assert doc_anon.tables[0].cell(1, 0).text.strip() == "[MONEY_2]"
    assert doc_anon.tables[0].cell(1, 1).text.strip() == "руб."

    final_lines = [
        ln.replace("[MONEY_1]", "386 887,422")
        .replace("[MONEY_2]", "7 432 480,00")
        for ln in anon_lines
    ]
    final = await assembler.assemble(
        data, "t.docx", "\n".join(final_lines), parsed.structure,
        strip_hf_images=False, scrub_metadata=False)
    doc_final = Document(io.BytesIO(final))
    assert "386 887,422 руб." in doc_final.paragraphs[0].text
    assert "7 432 480,00" in doc_final.tables[0].cell(1, 0).text
    assert doc_final.tables[0].cell(1, 1).text.strip() == "руб."
    print("TEST OK: docx money round-trip — суммы с запятой не искажаются")


async def test_xlsx_financial_format_roundtrip():
    """Багрепорт 2026-09-11 (сессия 5aecf575, формат «Финансовый»): числовые
    ячейки с accounting-форматом (_-* #,##0.00₽) извлекаются как str(float)
    («1586238.422») — без разрядов и валюты — и MONEY-regex их не ловил.
    Теперь сегмент рендерится «как в Excel» (1 586 238,422 ₽), и все суммы
    скрываются; после де-анонимизации ячейки остаются числами с исходными
    значениями."""
    fmt = '_-* #,##0.00\\ _₽_-;\\-* #,##0.00\\ _₽_-;_-* "-"??\\ _₽_-;_-@_-'
    buf = io.BytesIO()
    wb = Workbook()
    ws = wb.active
    ws["A1"] = "Статья"
    c1 = ws["B1"]
    c1.value = 1586238.422
    c1.number_format = fmt
    c2 = ws["B2"]
    c2.value = 793119.211
    c2.number_format = fmt
    c3 = ws["C1"]
    c3.value = 1586238.422
    c3.number_format = fmt
    ws["A3"] = "ИНН"
    ws["C3"] = 7701234567  # General — не трогаем
    wb.save(buf)
    data = buf.getvalue()

    parser = FileParser()
    assembler = FileAssembler()
    parsed = await parser.parse(data, "t.xlsx")
    # сегменты отрендерены «как в Excel» (группировка + запятая + валюта)
    assert "1 586 238,422" in parsed.text, parsed.text
    assert "793 119,211" in parsed.text, parsed.text
    ner = NERService()
    ents, _, failed = await ner.extract_entities_detailed(
        parsed.text, use_llm=False)
    money = [e for e in ents if e.type == "MONEY"]
    # три ячейки с финансовым форматом; одинаковые значения в B1 и C1
    # дают отдельные сущности-вхождения, но один токен
    assert len(money) == 3, [e.text for e in money]
    assert not failed

    from anonymizer_proxy.anonymizer.replacer import TextReplacer
    seq = {"n": 0}

    async def add_mapping(value, etype):
        seq["n"] += 1
        return f"[{etype}_{seq['n']}]"

    anon_text, mappings = await TextReplacer().anonymize(
        parsed.text, money, add_mapping)
    anon = await assembler.assemble(
        data, "t.xlsx", anon_text, parsed.structure)
    wb_anon = load_workbook(io.BytesIO(anon))
    assert wb_anon.active["B1"].value.startswith("[MONEY_")
    assert wb_anon.active["B2"].value.startswith("[MONEY_")
    assert wb_anon.active["C1"].value.startswith("[MONEY_")
    assert wb_anon.active["C3"].value == 7701234567  # General — не тронут

    mapping_dict = {m.token: m.original_value for m in mappings}
    assert len(mapping_dict) == 2  # B1 и C1 — одно значение
    deanon_text = await TextReplacer().deanonymize(anon_text, mapping_dict)
    final = await assembler.assemble(
        data, "t.xlsx", deanon_text, parsed.structure)
    wb_final = load_workbook(io.BytesIO(final))
    ws_f = wb_final.active
    assert isinstance(ws_f["B1"].value, float)
    assert abs(ws_f["B1"].value - 1586238.422) < 1e-9
    assert isinstance(ws_f["B2"].value, float)
    assert abs(ws_f["B2"].value - 793119.211) < 1e-9
    assert isinstance(ws_f["C1"].value, float)
    assert abs(ws_f["C1"].value - 1586238.422) < 1e-9
    assert ws_f["C3"].value == 7701234567
    print("TEST OK: xlsx financial format round-trip — все суммы скрыты "
          "и восстановлены числами")


async def main():
    await test_docx_table_structure()
    await test_docx_multiparagraph_cell()
    await test_xlsx_types_and_formulas()
    await test_xlsx_empty_cells()
    fa2, content, parsed2, anon, out = (
        await test_docx_headers_footers_textboxes_images())
    await check_docx_headers_footers_result(fa2, content, parsed2, anon, out)
    await test_hyperlink_targets_scrubbed()
    await test_page_number_fields()
    await test_docx_nested_tables_and_comments()
    await test_xlsx_comments_roundtrip()
    await test_xlsx_numeric_roundtrip()
    await test_docx_money_roundtrip()
    await test_xlsx_financial_format_roundtrip()
    print("\nALL FILE PARSER TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
