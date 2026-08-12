"""
Парсеры файлов для извлечения текста
Поддерживаемые форматы: DOCX, XLSX, XML (MS Project)
"""
import io
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field

from docx import Document
from openpyxl import load_workbook


@dataclass
class ParsedContent:
    """Результат парсинга файла"""
    text: str = ""
    structure: dict = field(default_factory=dict)  # Сохранение структуры для обратной сборки
    metadata: dict = field(default_factory=dict)


def _normalize_line(text: str) -> str:
    """Одна строка текста: без переносов (иначе обратная сборка по
    split('\n') даст смещение)"""
    return re.sub(r"\s*\n+\s*", " ", text or "").strip()


def _cell_to_line(value) -> str:
    """Значение ячейки Excel -> одна строка текста (пустая для пустых)"""
    if value is None:
        return ""
    return _normalize_line(str(value))


def _set_paragraph_text(para, text: str) -> None:
    """Заменить текст абзаца, сохранив сам абзац и его стиль.
    Неизменённые абзацы не затрагиваются."""
    if _normalize_line(para.text) == text:
        return
    if para.runs:
        for run in para.runs:
            run.text = ""
        para.runs[0].text = text
    else:
        para.add_run(text)


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
            text = _normalize_line(para.text)
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
                        text = _normalize_line(para.text)
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

        return ParsedContent(
            text="\n".join(segments),
            structure=structure,
            metadata={
                "format": "docx",
                "paragraphs_count": len(doc.paragraphs),
                "tables_count": len(doc.tables)
            }
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

        return ParsedContent(
            text="\n".join(segments),
            structure=structure,
            metadata={"format": "xlsx", "sheets_count": len(wb.sheetnames)}
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
            }
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
            metadata={"format": "text", "encoding": "auto-detected"}
        )


class FileAssembler:
    """Сборщик файлов обратно из анонимизированного текста"""
    
    async def assemble(
        self, 
        original_content: bytes, 
        original_filename: str,
        anonymized_text: str,
        structure: dict
    ) -> bytes:
        """
        Собрать файл обратно с анонимизированным содержимым
        
        Args:
            original_content: Оригинальное содержимое файла
            original_filename: Имя файла
            anonymized_text: Анонимизированный текст
            structure: Структура из ParsedContent
        
        Returns:
            Бинарное содержимое анонимизированного файла
        """
        ext = Path(original_filename).suffix.lower()
        
        if ext == ".docx":
            return self._assemble_docx(original_content, anonymized_text, structure)
        elif ext == ".xlsx":
            return self._assemble_xlsx(original_content, anonymized_text, structure)
        elif ext == ".xml":
            return self._assemble_xml(original_content, anonymized_text, structure)
        elif ext in (".txt", ".md"):
            return anonymized_text.encode("utf-8")
        else:
            raise ValueError(f"Неподдерживаемый формат файла: {ext}")
    
    def _assemble_docx(self, original_content: bytes, anonymized_text: str, structure: dict) -> bytes:
        """Сборка DOCX файла: текст записывается обратно по координатам,
        сохранённым при парсинге (абзацы тела и абзацы ячеек таблиц).
        Сетка таблиц и стили абзацев сохраняются; неизменённые абзацы
        не затрагиваются."""
        doc = Document(io.BytesIO(original_content))
        parts = anonymized_text.split("\n")
        segments = structure.get("segments", [])

        for seg, part in zip(segments, parts):
            if seg["kind"] == "para":
                para = doc.paragraphs[seg["para"]]
            else:  # cell_para
                cell = doc.tables[seg["table"]].rows[seg["row"]].cells[seg["cell"]]
                para = cell.paragraphs[seg["para"]]
            _set_paragraph_text(para, part)

        # Сохраняем в bytes
        output = io.BytesIO()
        doc.save(output)
        return output.getvalue()

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
