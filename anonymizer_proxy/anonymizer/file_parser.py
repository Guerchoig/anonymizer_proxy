"""
Парсеры файлов для извлечения текста
Поддерживаемые форматы: DOCX, XLSX, XML (MS Project)
"""
import io
import os
import re
import zipfile
import posixpath
import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph
from openpyxl import load_workbook

from ..config import logger


@dataclass
class ParsedContent:
    """Результат парсинга файла"""
    text: str = ""
    structure: dict = field(default_factory=dict)  # Сохранение структуры для обратной сборки
    metadata: dict = field(default_factory=dict)
    markdown: str = ""  # Форматированное markdown-представление (с таблицами)


def _normalize_line(text: str) -> str:
    """Одна строка текста: без переносов (иначе обратная сборка по
    split('\n') даст смещение)"""
    return re.sub(r"\s*\n+\s*", " ", text or "").strip()


def _cell_to_line(value) -> str:
    """Значение ячейки Excel -> одна строка текста (пустая для пустых)"""
    if value is None:
        return ""
    return _normalize_line(str(value))


def _fmt_cell_value(value) -> str:
    """Значение ячейки Excel в читаемом виде (markdown, read-column).

    Даты — ISO (YYYY-MM-DD, без шума «00:00:00»), escape _x000D_ (CR,
    попадающий в текст при Excel-экспорте многострочных заголовков) —
    убирается, переносы строк сворачиваются (markdown-таблица обязана
    быть однострочной). Используется ТОЛЬКО для отображения: сегменты
    parse/assemble (_cell_to_line) остаются как есть — обратная сборка
    по 1:1-сравнению не ломается.
    """
    if value is None:
        return ""
    if isinstance(value, datetime):
        if (value.hour, value.minute, value.second, value.microsecond) == (0, 0, 0, 0):
            return value.strftime("%Y-%m-%d")
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    return _normalize_line(re.sub(r"_x000D_", "", str(value)))


def _rows_to_markdown(rows: list[list[str]]) -> str:
    """Список списков строк -> markdown-таблица (первая строка — заголовок)."""
    if not rows:
        return ""
    header = rows[0]
    col_count = max((len(r) for r in rows), default=len(header))
    lines = ["| " + " | ".join((header + [""] * col_count)[:col_count]) + " |"]
    lines.append("|" + " --- |" * col_count)
    for r in rows[1:]:
        cells = (r + [""] * col_count)[:col_count]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _nearest_txbx(node):
    """Ближайший контейнер w:txbxContent для узла (или None)."""
    for anc in node.iterancestors(qn("w:txbxContent")):
        return anc
    return None


def _scrub_docx_metadata(doc) -> None:
    """Обезличить метаданные пакета DOCX.

    docProps/core.xml хранит автора и даты правок, docProps/app.xml —
    компанию, менеджера и гиперссылки заголовков (HLinks): в
    анонимизированной копии это такая же утечка PII, как текст документа.
    Вызывается только при АНОНИМИЗИРУЮЩЕЙ сборке (не при де-анонимизации).
    """
    try:
        cp = doc.core_properties
        cp.author = ""
        cp.last_modified_by = ""
        cp.comments = ""
        cp.subject = ""
        cp.keywords = ""
        cp.category = ""
    except Exception as exc:  # noqa: BLE001 - метаданные не должны ронять сборку
        logger.warning("Не удалось очистить core-properties: %s", exc)

    try:
        for part in doc.part.package.iter_parts():
            if str(part.partname) != "/docProps/app.xml":
                continue
            xml = part.blob.decode("utf-8", errors="ignore")
            cleaned = re.sub(r"<HLinks>.*?</HLinks>", "", xml, flags=re.S)
            cleaned = re.sub(r"<Company>.*?</Company>", "", cleaned, flags=re.S)
            cleaned = re.sub(r"<Manager>.*?</Manager>", "", cleaned, flags=re.S)
            if cleaned != xml:
                part._blob = cleaned.encode("utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не удалось очистить docProps/app.xml: %s", exc)


def _p_element_text(p_el) -> str:
    """Полный текст абзаца по его XML-элементу: включает содержимое
    гиперссылок (w:hyperlink), которое теряет python-docx .text, и
    исключает текст чужих/вложенных фигур — у абзаца внутри фигуры
    учитывается только её собственный w:txbxContent."""
    own_box = _nearest_txbx(p_el)
    parts = []
    for node in p_el.iter(qn("w:t")):
        if _nearest_txbx(node) is not own_box:
            continue
        parts.append(node.text or "")
    return "".join(parts)


def _table_to_markdown(table) -> str:
    """docx Table -> markdown-таблица (сохраняет сетку ячеек)."""
    rows = []
    for row in table.rows:
        cells = []
        for cell in row.cells:
            cell_text = "\n".join(
                _p_element_text(p._p) for p in cell.paragraphs)
            cells.append(_normalize_line(cell_text))
        rows.append(cells)
    return _rows_to_markdown(rows)


def _set_paragraph_text(para, text: str) -> None:
    """Заменить текст абзаца (включая текст гиперссылок), сохранив сам
    абзац и его стиль. Неизменённые абзацы не затрагиваются. Текст внутри
    вложенных фигур не трогается — у него свои сегменты."""
    p_el = para._p
    if _normalize_line(_p_element_text(p_el)) == text:
        return
    own_box = _nearest_txbx(p_el)

    # Поля Word (PAGE, NUMPAGES, перекрёстные ссылки...): в изменённом
    # абзаце структура полей удаляется. Word вычисляет значения полей при
    # рендере и дорисовывает их поверх анонимизированного текста — футер
    # «Страница 2 из 27» после маскировки превращается в «[LOC_11]227»
    # (токен + «живые» номера страницы). Неизменённые абзацы не затрагиваются
    # (ранний выход выше) — поля там работают как раньше, а де-анонимизация
    # возвращает исходный текст, и поля оживают снова.
    for r in list(p_el.iter(qn("w:r"))):
        if _nearest_txbx(r) is not own_box:
            continue
        if (r.find(qn("w:fldChar")) is not None
                or r.find(qn("w:instrText")) is not None):
            parent = r.getparent()
            if parent is not None:
                parent.remove(r)
    for fs in list(p_el.iter(qn("w:fldSimple"))):
        if _nearest_txbx(fs) is own_box:
            parent = fs.getparent()
            if parent is not None:
                parent.remove(fs)

    targets = [
        t for t in p_el.iter(qn("w:t"))
        if _nearest_txbx(t) is own_box
    ]
    if targets:
        for t in targets:
            t.text = ""
        first = targets[0]
        first.text = text
        first.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    else:
        para.add_run(text)


# ==================== DOCX: колонтитулы, фигуры, картинки ====================

# Теги с изображениями (Clark-нотация; префиксы nsmap python-docx не
# гарантирует для pic/a/v, поэтому используем явные URI)
_PIC_TAG = "{http://schemas.openxmlformats.org/drawingml/2006/picture}pic"
_BLIP_TAG = "{http://schemas.openxmlformats.org/drawingml/2006/main}blip"
_IMAGEDATA_TAG = "{urn:schemas-microsoft-com:vml}imagedata"

# Удалять картинки (логотипы) из колонтитулов анонимизированных DOCX.
# Отключается REMOVE_HEADER_IMAGES=0
REMOVE_HF_IMAGES = os.getenv(
    "REMOVE_HEADER_IMAGES", "1"
).strip().lower() in ("1", "true", "yes", "on")


def _docx_hf_objects(doc):
    """Уникальные части колонтитулов DOCX в детерминированном порядке.

    Пропускаются колонтитулы «как в предыдущем разделе» (нет собственной
    части) и повторные ссылки разных секций на одну и ту же часть — иначе
    сегменты задвоятся и нарушится соответствие «строка текста <-> сегмент»,
    на котором строится обратная сборка.
    """
    seen = set()
    result = []
    for section in doc.sections:
        for hf in (
            section.header,
            section.footer,
            section.first_page_header,
            section.first_page_footer,
            section.even_page_header,
            section.even_page_footer,
        ):
            try:
                if hf.is_linked_to_previous:
                    continue
            except Exception:  # noqa: BLE001 - битая ссылка не должна ронять парсинг
                continue
            partname = str(hf.part.partname)
            if partname in seen:
                continue
            seen.add(partname)
            result.append(hf)
    return result


def _top_txbx_contents(root):
    """Корневые w:txbxContent (без вложенных) в порядке следования.

    Вложенные фигуры обрабатываются вместе с внешним контейнером, чтобы
    каждый абзац попал в сегменты ровно один раз.
    """
    result = []
    for el in root.iter(qn("w:txbxContent")):
        if any(True for _ in el.iterancestors(qn("w:txbxContent"))):
            continue
        result.append(el)
    return result


def _txbx_para_elements(root):
    """Для каждого корневого w:txbxContent — список его элементов w:p.

    Используется и при парсинге, и при сборке — порядок индексов обязан
    совпадать (детерминированный обход lxml).
    """
    return [list(content.iter(qn("w:p"))) for content in _top_txbx_contents(root)]


def _strip_hf_images(doc) -> int:
    """Удалить картинки из всех частей колонтитулов.

    Затрагиваются только узлы с изображениями: w:drawing, содержащие
    pic:pic или a:blip, и w:pict с v:imagedata. Фигуры-надписи без
    картинок не удаляются — их текст уже анонимизирован.
    Попутно удаляются Relationship-записи удалённых картинок, иначе
    media-файлы остаются в пакете (связь делает их «используемыми»).
    """
    removed = 0
    for hf in _docx_hf_objects(doc):
        root = hf.part.element
        targets = []  # собираем заранее: удаление во время iter небезопасно
        rids = set()  # rId картинок для последующей чистки .rels части
        for el in root.iter():
            if el.tag == qn("w:drawing"):
                if (el.find(f".//{_PIC_TAG}") is not None
                        or el.find(f".//{_BLIP_TAG}") is not None):
                    targets.append(el)
            elif el.tag == qn("w:pict"):
                if el.find(f".//{_IMAGEDATA_TAG}") is not None:
                    targets.append(el)
        for el in targets:
            for node in el.iter():
                for attr in (qn("r:embed"), qn("r:id"), qn("r:link")):
                    rid = node.get(attr)
                    if rid:
                        rids.add(rid)
            parent = el.getparent()
            if parent is not None:
                parent.remove(el)
                removed += 1
        for rid in rids:
            try:
                hf.part.drop_rel(rid)
            except (KeyError, AttributeError):
                # Связи может не быть (external) либо старая версия python-docx
                logger.debug("Не удалось удалить связь %s части %s",
                             rid, hf.part.partname)
    return removed


def _drop_orphan_media(docx_bytes: bytes) -> tuple[bytes, int]:
    """Вымыть из DOCX-пакета media-файлы без ссылок в .rels.

    После удаления картинок из колонтитулов их байты остаются лежать в
    word/media/ — это утечка (логотипы читаются из ZIP напрямую).
    Попутно чистятся осиротевшие Relationship-записи, чтобы пакет
    оставался валидным. Возвращает (новые байты, число удалённых файлов).
    """
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zin:
        infos = zin.infolist()
        data = {i.filename: zin.read(i.filename) for i in infos}

    def resolve(base_dir: str, target: str) -> str:
        return posixpath.normpath(posixpath.join(base_dir, target)).lstrip("/")

    # 1) Какие части реально используются (все .rels, только Internal)
    used = set()
    for name, blob in data.items():
        if not name.endswith(".rels"):
            continue
        base_dir = posixpath.dirname(posixpath.dirname(name))
        root = ET.fromstring(blob)
        for rel in root:
            if (rel.get("TargetMode") or "Internal").lower() == "external":
                continue
            used.add(resolve(base_dir, rel.get("Target", "")))

    dropped = {
        n for n in data
        if n.startswith("word/media/") and n not in used
    }
    if not dropped:
        return docx_bytes, 0

    # 2) Чистим .rels от связей с удалёнными частями
    for name in list(data):
        if not name.endswith(".rels"):
            continue
        base_dir = posixpath.dirname(posixpath.dirname(name))
        root = ET.fromstring(data[name])
        changed = False
        for rel in list(root):
            tmode = (rel.get("TargetMode") or "Internal").lower()
            resolved = (rel.get("Target", "") if tmode == "external"
                        else resolve(base_dir, rel.get("Target", "")))
            if resolved in dropped:
                root.remove(rel)
                changed = True
        if changed:
            data[name] = ET.tostring(
                root, xml_declaration=True, encoding="UTF-8")

    # 3) Перезаписываем пакет без удалённых media
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
        for i in infos:
            if i.filename in dropped:
                continue
            zout.writestr(i, data[i.filename])
    return buf.getvalue(), len(dropped)


def _scrub_hyperlink_targets(docx_bytes: bytes) -> tuple[bytes, int]:
    """Заменить внешние цели гиперссылок в *.rels пакета на нейтральное «#».

    Адрес сайта/почты может храниться не только в тексте (его маскирует
    NER-слой), но и в Relationship-целях гиперссылок: тогда он утечёт из
    анонимизированной копии даже при замаскированном тексте ссылки.
    Возвращает (новые байты пакета, число обезличенных ссылок).
    Вызывается только при АНОНИМИЗИРУЮЩЕЙ сборке (не при де-анонимизации).
    """
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zin:
        infos = zin.infolist()
        data = {i.filename: zin.read(i.filename) for i in infos}

    changed = 0
    for name, blob in data.items():
        if not name.endswith(".rels"):
            continue
        try:
            root = ET.fromstring(blob)
        except ET.ParseError:
            continue
        modified = False
        for rel in root:
            if (rel.get("TargetMode") or "Internal").lower() != "external":
                continue
            if "hyperlink" not in (rel.get("Type") or "").lower():
                continue
            target = rel.get("Target") or ""
            if re.match(r"(?i)^(https?://|ftp://|mailto:|www\.)", target):
                rel.set("Target", "#")
                modified = True
                changed += 1
        if modified:
            data[name] = ET.tostring(
                root, xml_declaration=True, encoding="UTF-8")

    if not changed:
        return docx_bytes, 0

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
        for i in infos:
            zout.writestr(i, data[i.filename])
    return buf.getvalue(), changed


class FileParser:
    """Парсер файлов различных форматов"""
    
    SUPPORTED_EXTENSIONS = {".docx", ".xlsx", ".xml", ".txt", ".md"}
    
    @classmethod
    def is_supported(cls, filename: str) -> bool:
        """Проверить, поддерживается ли формат файла"""
        ext = Path(filename).suffix.lower()
        return ext in cls.SUPPORTED_EXTENSIONS
    
    @classmethod
    def get_content_type(cls, filename: str) -> str:
        """Определить тип контента по расширению"""
        ext = Path(filename).suffix.lower()
        content_types = {
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".xml": "application/xml",
            ".txt": "text/plain",
            ".md": "text/markdown",
        }
        return content_types.get(ext, "application/octet-stream")
    
    async def parse(self, content: bytes, filename: str) -> ParsedContent:
        """
        Распарсить содержимое файла
        
        Args:
            content: Бинарное содержимое файла
            filename: Имя файла (для определения формата)
        
        Returns:
            ParsedContent с извлечённым текстом и структурой
        """
        ext = Path(filename).suffix.lower()
        
        if ext == ".docx":
            return self._parse_docx(content)
        elif ext == ".xlsx":
            return self._parse_xlsx(content)
        elif ext == ".xml":
            return self._parse_xml(content)
        elif ext in (".txt", ".md"):
            return self._parse_text(content)
        else:
            raise ValueError(f"Неподдерживаемый формат файла: {ext}")
    
    def _parse_docx(self, content: bytes) -> ParsedContent:
        """Парсинг DOCX файлов с сохранением структуры абзацев и таблиц.

        Каждый абзац тела документа и каждый абзац ячейки таблицы дают
        ровно одну строку текста (сегмент); координаты сегментов
        сохраняются в structure["segments"] для обратной сборки.
        """
        doc = Document(io.BytesIO(content))

        segments = []
        structure = {"paragraphs": [], "tables": [], "segments": []}

        # Извлекаем параграфы тела документа
        for i, para in enumerate(doc.paragraphs):
            text = _normalize_line(_p_element_text(para._p))
            if text:
                segments.append(text)
                structure["segments"].append({"kind": "para", "para": i})
                structure["paragraphs"].append({
                    "index": i,
                    "text": text,
                    "style": para.style.name if para.style else None
                })

        # Извлекаем таблицы: каждый абзац ячейки — отдельный сегмент
        # (текст ячейки может состоять из нескольких абзацев)
        for table_idx, table in enumerate(doc.tables):
            table_data = []
            for row_idx, row in enumerate(table.rows):
                row_data = []
                for cell_idx, cell in enumerate(row.cells):
                    cell_paras = []
                    for para_idx, para in enumerate(cell.paragraphs):
                        text = _normalize_line(_p_element_text(para._p))
                        if text:
                            segments.append(text)
                            structure["segments"].append({
                                "kind": "cell_para",
                                "table": table_idx,
                                "row": row_idx,
                                "cell": cell_idx,
                                "para": para_idx
                            })
                            cell_paras.append(text)
                    row_data.append("\n".join(cell_paras))
                table_data.append(row_data)
            structure["tables"].append({
                "table_index": table_idx,
                "data": table_data
            })

        # Колонтитулы: абзацы и ячейки таблиц каждой уникальной части
        structure["headers_footers"] = []
        for hf in _docx_hf_objects(doc):
            pn = str(hf.part.partname)
            title = Path(pn).name  # например header1.xml
            entry = {
                "part": pn,
                "title": title,
                "label": "Верхний колонтитул" if "header" in title.lower()
                         else "Нижний колонтитул",
                "paras": [],
            }
            for i, para in enumerate(hf.paragraphs):
                text = _normalize_line(_p_element_text(para._p))
                if text:
                    segments.append(text)
                    structure["segments"].append(
                        {"kind": "hf_para", "part": pn, "para": i})
                    entry["paras"].append(text)
            for ti, table in enumerate(hf.tables):
                for ri, row in enumerate(table.rows):
                    for ci, cell in enumerate(row.cells):
                        for pi, para in enumerate(cell.paragraphs):
                            text = _normalize_line(_p_element_text(para._p))
                            if text:
                                segments.append(text)
                                structure["segments"].append({
                                    "kind": "hf_cell_para",
                                    "part": pn,
                                    "table": ti, "row": ri,
                                    "cell": ci, "para": pi,
                                })
                                entry["paras"].append(text)
            if entry["paras"]:
                structure["headers_footers"].append(entry)

        # Текстовые фигуры (w:txbxContent): тело документа и колонтитулы.
        # Дубли в mc:Choice/mc:Fallback дают отдельные сегменты — замена
        # согласована, т.к. маппинг value->token глобальный по сессии.
        structure["textboxes"] = []
        hosts = [("document", doc.element)]
        for hf in _docx_hf_objects(doc):
            hosts.append((str(hf.part.partname), hf.part.element))
        for host_name, host_root in hosts:
            for k, p_elements in enumerate(_txbx_para_elements(host_root)):
                shape_entry = {"host": host_name, "index": k, "paras": []}
                for j, p_el in enumerate(p_elements):
                    text = _normalize_line(_p_element_text(p_el))
                    if text:
                        segments.append(text)
                        structure["segments"].append({
                            "kind": "txbx_para",
                            "host": host_name,
                            "txbx": k, "para": j,
                        })
                        shape_entry["paras"].append(text)
                if shape_entry["paras"]:
                    structure["textboxes"].append(shape_entry)

        # Markdown-представление с таблицами (для review и облака):
        # абзацы и таблицы в порядке документа, таблицы -> markdown-таблицы;
        # затем блоки колонтитулов и фигур, чтобы review отражал анонимизацию
        markdown_parts = []
        for child in doc.element.body.iterchildren():
            if child.tag == qn("w:p"):
                text = _normalize_line(_p_element_text(child))
                if text:
                    markdown_parts.append(text)
            elif child.tag == qn("w:tbl"):
                table = Table(child, doc)
                markdown_parts.append(_table_to_markdown(table))

        extra_md = []
        for entry in structure["headers_footers"]:
            extra_md.append(
                f"**[{entry['label']} {entry['title']}]**\n"
                + "\n".join(entry["paras"]))
        for entry in structure["textboxes"]:
            extra_md.append(
                f"**[Фигура {entry['index']} ({entry['host']})]**\n"
                + "\n".join(entry["paras"]))
        if extra_md:
            markdown_parts.extend(extra_md)

        markdown = "\n\n".join(p for p in markdown_parts if p)

        return ParsedContent(
            text="\n".join(segments),
            structure=structure,
            metadata={
                "format": "docx",
                "paragraphs_count": len(doc.paragraphs),
                "tables_count": len(doc.tables),
                "headers_footers_count": len(structure["headers_footers"]),
                "textboxes_count": len(structure["textboxes"]),
            },
            markdown=markdown,
        )

    def _parse_xlsx(self, content: bytes) -> ParsedContent:
        """Парсинг XLSX файлов с сохранением структуры листов и ячеек.

        Ячейки читаются как есть (data_only=False): формулы остаются
        формулами — при обратной сборке неизменённые ячейки (формулы,
        числа, даты) не перезаписываются. Координаты непустых ячеек
        сохраняются в structure["segments"].
        """
        wb = load_workbook(io.BytesIO(content))

        segments = []
        structure = {"sheets": [], "segments": []}

        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            sheet_data = {"name": sheet_name, "rows": []}

            for row in ws.iter_rows():
                row_values = []
                for cell in row:
                    cell_str = _cell_to_line(cell.value)
                    if cell_str:
                        row_values.append(cell_str)
                        segments.append(cell_str)
                        structure["segments"].append({
                            "kind": "cell",
                            "sheet": sheet_name,
                            "row": cell.row,
                            "col": cell.column
                        })
                    else:
                        row_values.append("")
                sheet_data["rows"].append(row_values)

            structure["sheets"].append(sheet_data)

        # Markdown-представление: каждый лист -> markdown-таблица
        # (дисплейное представление через _fmt_cell_value: даты ISO,
        # без _x000D_ — сегменты text остаются через _cell_to_line)
        markdown_parts = []
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            rows = []
            for row in ws.iter_rows():
                cells = [_fmt_cell_value(cell.value) for cell in row]
                if not any(cells):
                    continue
                rows.append(cells)
            markdown_parts.append(f"## {sheet_name}\n\n{_rows_to_markdown(rows)}")
        markdown = "\n\n".join(markdown_parts)

        return ParsedContent(
            text="\n".join(segments),
            structure=structure,
            metadata={"format": "xlsx", "sheets_count": len(wb.sheetnames)},
            markdown=markdown,
        )

    def _parse_xml(self, content: bytes) -> ParsedContent:
        """
        Парсинг XML файлов (включая MS Project XML)
        Извлекает текстовое содержимое, сохраняя структуру тегов
        """
        try:
            root = ET.fromstring(content)
        except ET.ParseError as e:
            raise ValueError(f"Ошибка парсинга XML: {e}")
        
        text_parts = []
        structure = {"elements": []}
        
        # Рекурсивно извлекаем текст из всех элементов
        def extract_text(element, path=""):
            current_path = f"{path}/{element.tag}" if path else element.tag
            
            # Текст элемента
            if element.text and element.text.strip():
                text = element.text.strip()
                text_parts.append(text)
                structure["elements"].append({
                    "path": current_path,
                    "text": text,
                    "type": "text"
                })
            
            # Атрибуты (могут содержать имена, названия)
            for attr_name, attr_value in element.attrib.items():
                if attr_value and len(attr_value) > 2:  # Игнорируем короткие значения
                    # Проверяем, может ли атрибут содержать PII
                    pii_attrs = ["name", "Name", "ResourceName", "Author", "Manager"]
                    if any(pii in attr_name for pii in pii_attrs):
                        text_parts.append(attr_value)
                        structure["elements"].append({
                            "path": f"{current_path}@{attr_name}",
                            "text": attr_value,
                            "type": "attribute"
                        })
            
            # Рекурсия для дочерних элементов
            for child in element:
                extract_text(child, current_path)
            
            # Tail текст (текст после закрывающего тега)
            if element.tail and element.tail.strip():
                tail_text = element.tail.strip()
                text_parts.append(tail_text)
                structure["elements"].append({
                    "path": f"{current_path}/tail",
                    "text": tail_text,
                    "type": "tail"
                })
        
        extract_text(root)
        
        full_text = "\n".join(text_parts)
        
        # Определяем тип XML (MS Project или другой)
        xml_type = "generic"
        if root.tag.endswith("Project") or "Project" in root.tag:
            xml_type = "msproject"
        
        return ParsedContent(
            text=full_text,
            structure=structure,
            metadata={
                "format": "xml",
                "xml_type": xml_type,
                "root_tag": root.tag,
                "elements_count": len(structure["elements"])
            },
            markdown=full_text,
        )
    
    def _parse_text(self, content: bytes) -> ParsedContent:
        """Парсинг текстовых файлов"""
        # Пытаемся определить кодировку
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = content.decode("cp1251")  # Windows-1251 для русского
            except UnicodeDecodeError:
                text = content.decode("utf-8", errors="replace")
        
        return ParsedContent(
            text=text,
            structure={"type": "plain_text"},
            metadata={"format": "text", "encoding": "auto-detected"},
            markdown=text,
        )


class FileAssembler:
    """Сборщик файлов обратно из анонимизированного текста"""
    
    async def assemble(
        self,
        original_content: bytes,
        original_filename: str,
        anonymized_text: str,
        structure: dict,
        strip_hf_images: bool = True,
        scrub_metadata: bool = True,
    ) -> bytes:
        """
        Собрать файл обратно с анонимизированным содержимым

        Args:
            original_content: Оригинальное содержимое файла
            original_filename: Имя файла
            anonymized_text: Анонимизированный текст
            structure: Структура из ParsedContent
            strip_hf_images: Удалять картинки колонтитулов DOCX (логотипы);
                при де-анонимизации передавайте False — восстановленный
                файл должен остаться с оригинальным оформлением
            scrub_metadata: Обезличивать метаданные пакета DOCX
                (автор, компания, HLinks); False — для де-анонимизации

        Returns:
            Бинарное содержимое файла с заменённым текстом
        """
        ext = Path(original_filename).suffix.lower()
        
        if ext == ".docx":
            return self._assemble_docx(
                original_content, anonymized_text, structure,
                strip_hf_images=strip_hf_images,
                scrub_metadata=scrub_metadata,
            )
        elif ext == ".xlsx":
            return self._assemble_xlsx(original_content, anonymized_text, structure)
        elif ext == ".xml":
            return self._assemble_xml(original_content, anonymized_text, structure)
        elif ext in (".txt", ".md"):
            return anonymized_text.encode("utf-8")
        else:
            raise ValueError(f"Неподдерживаемый формат файла: {ext}")
    
    def _assemble_docx(
        self,
        original_content: bytes,
        anonymized_text: str,
        structure: dict,
        strip_hf_images: bool = True,
        scrub_metadata: bool = True,
    ) -> bytes:
        """Сборка DOCX файла: текст записывается обратно по координатам,
        сохранённым при парсинге (абзацы тела, ячейки таблиц, колонтитулы,
        текстовые фигуры). Сетка таблиц и стили абзацев сохраняются;
        неизменённые абзацы не затрагиваются.
        Из колонтитулов удаляются картинки (логотипы), а их media-файлы
        вымываются из пакета (см. REMOVE_HEADER_IMAGES)."""
        doc = Document(io.BytesIO(original_content))
        parts = anonymized_text.split("\n")
        segments = structure.get("segments", [])

        # Быстрый доступ к частям колонтитулов по имени части
        hf_by_part = {}
        hosts = {"document": doc.element}
        for hf in _docx_hf_objects(doc):
            pn = str(hf.part.partname)
            hf_by_part[pn] = hf
            hosts[pn] = hf.part.element

        for seg, part in zip(segments, parts):
            kind = seg["kind"]
            if kind == "para":
                para = doc.paragraphs[seg["para"]]
            elif kind == "cell_para":
                cell = doc.tables[seg["table"]].rows[seg["row"]].cells[seg["cell"]]
                para = cell.paragraphs[seg["para"]]
            elif kind == "hf_para":
                hf = hf_by_part.get(seg["part"])
                if hf is None:
                    logger.warning(
                        "Пропущен сегмент колонтитула %s: часть недоступна",
                        seg["part"])
                    continue
                para = hf.paragraphs[seg["para"]]
            elif kind == "hf_cell_para":
                hf = hf_by_part.get(seg["part"])
                if hf is None:
                    logger.warning(
                        "Пропущен сегмент колонтитула %s: часть недоступна",
                        seg["part"])
                    continue
                cell = hf.tables[seg["table"]].rows[seg["row"]].cells[seg["cell"]]
                para = cell.paragraphs[seg["para"]]
            elif kind == "txbx_para":
                boxes = _txbx_para_elements(hosts.get(seg["host"], doc.element))
                try:
                    p_el = boxes[seg["txbx"]][seg["para"]]
                except IndexError:
                    logger.warning(
                        "Пропущен сегмент фигуры (%s/%s): индекс вне диапазона",
                        seg["host"], seg["txbx"])
                    continue
                para = Paragraph(p_el, None)
            else:
                logger.warning("Неизвестный тип сегмента: %s", kind)
                continue
            _set_paragraph_text(para, part)

        if scrub_metadata:
            _scrub_docx_metadata(doc)

        # Удаляем картинки (логотипы) из колонтитулов и вымываем их
        # байты из пакета — иначе логотипы остаются в word/media/
        do_strip = strip_hf_images and REMOVE_HF_IMAGES
        removed_images = 0
        if do_strip:
            removed_images = _strip_hf_images(doc)

        # Сохраняем в bytes
        output = io.BytesIO()
        doc.save(output)
        result = output.getvalue()

        if scrub_metadata:
            # Внешние цели гиперссылок (адреса сайтов, mailto:) не должны
            # оставаться в анонимизированной копии даже при замаскированном
            # тексте ссылки
            result, scrubbed_links = _scrub_hyperlink_targets(result)
            if scrubbed_links:
                logger.info(
                    "Обезличено внешних гиперссылок в пакете: %d",
                    scrubbed_links)

        if do_strip and removed_images:
            result, dropped = _drop_orphan_media(result)
            logger.info(
                "Удалено картинок из колонтитулов: %s (media-файлов из "
                "пакета: %s)", removed_images, dropped)
        return result

    def _assemble_xlsx(self, original_content: bytes, anonymized_text: str, structure: dict) -> bytes:
        """Сборка XLSX файла: значения записываются обратно по координатам,
        сохранённым при парсинге. Ячейки, чей текст не изменился, не
        затрагиваются — это сохраняет формулы, числа и даты."""
        wb = load_workbook(io.BytesIO(original_content))
        parts = anonymized_text.split("\n")
        segments = structure.get("segments", [])

        for seg, part in zip(segments, parts):
            ws = wb[seg["sheet"]]
            cell = ws.cell(row=seg["row"], column=seg["col"])
            if _cell_to_line(cell.value) != part:
                cell.value = part

        output = io.BytesIO()
        wb.save(output)
        return output.getvalue()

    def _assemble_xml(self, original_content: bytes, anonymized_text: str, structure: dict) -> bytes:
        """Сборка XML файла"""
        root = ET.fromstring(original_content)
        
        # Разбиваем анонимизированный текст на части
        anon_parts = anonymized_text.split("\n")
        part_idx = 0
        
        # Создаём маппинг path -> новый текст
        replacements = {}
        for elem_info in structure.get("elements", []):
            if part_idx < len(anon_parts):
                replacements[elem_info["path"]] = anon_parts[part_idx]
                part_idx += 1
        
        # Рекурсивно заменяем текст
        def replace_text(element, path=""):
            current_path = f"{path}/{element.tag}" if path else element.tag
            
            if element.text and element.text.strip():
                if current_path in replacements:
                    element.text = replacements[current_path]
            
            for attr_name, attr_value in element.attrib.items():
                attr_path = f"{current_path}@{attr_name}"
                if attr_path in replacements:
                    element.set(attr_name, replacements[attr_path])
            
            for child in element:
                replace_text(child, current_path)
            
            if element.tail and element.tail.strip():
                tail_path = f"{current_path}/tail"
                if tail_path in replacements:
                    element.tail = replacements[tail_path]
        
        replace_text(root)
        
        # ET не поддерживает pretty_print, используем indent для форматирования
        ET.indent(root, space="  ")
        return ET.tostring(root, encoding="unicode").encode("utf-8")
