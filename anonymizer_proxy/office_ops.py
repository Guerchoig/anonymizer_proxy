"""
office_ops — CLI-инструментарий правки документов MS Office без Python-кода.

Облачная модель вызывает готовые команды вместо написания скриптов на
python-docx/openpyxl (запуск из корня проекта):

  python -m anonymizer_proxy.office_ops list-tables --file "doc.docx"
  python -m anonymizer_proxy.office_ops dump --file "doc.docx" --format md
  python -m anonymizer_proxy.office_ops dump --file F.docx --find "текст" --out-file DUMP.md
  python -m anonymizer_proxy.office_ops read-column --file F.xlsx --column "Срок поручения"
  python -m anonymizer_proxy.office_ops read-column --file F.docx --table 0 --column 1
  python -m anonymizer_proxy.office_ops replace-text --file F --find "a" --replace "b" --output OUT
  python -m anonymizer_proxy.office_ops set-cell --file F --table 0 --row 1 --col 2 --text "..." --output OUT
  python -m anonymizer_proxy.office_ops add-row --file F --table 0 --cell "a" --cell "b" --output OUT
  python -m anonymizer_proxy.office_ops add-column --file F --table 0 --header "..." --cell "..." --output OUT
  python -m anonymizer_proxy.office_ops add-column --file F.docx --table 0 --column "Длительность" --position after --header "Примечание" --output OUT
  python -m anonymizer_proxy.office_ops add-column --file F.xlsx --column "Срок поручения" --position after --header "Поручение выдано" --cell "2026-05-19" --output OUT
  python -m anonymizer_proxy.office_ops delete-column --file F.xlsx --column "Комментарий" --output OUT
  python -m anonymizer_proxy.office_ops delete-column --file F.docx --table 0 --column 2 --output OUT
  python -m anonymizer_proxy.office_ops set-value --file F.xlsx --cell B2 --value 150 --output OUT
  python -m anonymizer_proxy.office_ops append-row --file F.xlsx --cell "a" --cell "b" --output OUT
  python -m anonymizer_proxy.office_ops apply --file F --from-text edit.txt --output OUT

Замены текста (replace-text, apply) выполняются через FileParser/FileAssembler:
сохраняются форматирование, стили, числа, даты и формулы — перезаписываются
только изменённые сегменты. Результат пишется в отдельный файл (--output)
или на место исходника (--in-place); по умолчанию исходник не трогается.

Цепочка правок анонимизированных документов: ПЕРВАЯ правка —
--file <name>.anonymized.<ext> --output <name>.result.<ext>; ВСЕ последующие
правки — --file <name>.result.<ext> --output <name>.result.<ext> (или
--in-place). Запись в <name>.anonymized.<ext> запрещена (правило исходника).
"""
import argparse
import asyncio
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path

try:
    from .anonymizer.file_parser import FileParser, FileAssembler, _fmt_cell_value
except ImportError:  # прямой запуск файла: python anonymizer_proxy\office_ops.py
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from anonymizer_proxy.anonymizer.file_parser import (
        FileParser, FileAssembler, _fmt_cell_value,
    )

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from openpyxl import load_workbook
from openpyxl.utils import column_index_from_string, get_column_letter

_parser = FileParser()
_assembler = FileAssembler()


# ==================== Общие помощники ====================

OFFICE_EXTS = {".docx", ".xlsx"}


def _corrected_office_name(path: Path) -> Path | None:
    """Правильное имя для файла результата с «перевёрнутым» расширением:
    «ЖКП…​.xlsx.result» -> «ЖКП…​.result.xlsx» (или None, если паттерн не тот)."""
    name = path.name
    if name.lower().endswith(".result"):
        base = name[: -len(".result")]  # «ЖКП….xlsx»
        base_low = base.lower()
        for ext in (".xlsx", ".docx"):
            if base_low.endswith(ext):
                stem = base[: -len(ext)]
                return path.with_name(stem + ".result" + ext)
    return None


def _result_name_for(src: Path) -> str:
    """Правильное имя файла результата для исходника: «F.xlsx» -> «F.result.xlsx»."""
    return src.stem + ".result" + src.suffix


def _read(path_str: str) -> tuple[Path, bytes]:
    path = Path(path_str)
    if not path.is_file():
        # Похожий файл с правильным именем? (частая путаница с .result)
        hint = ""
        if path.suffix.lower() not in OFFICE_EXTS:
            fixed = _corrected_office_name(path)
            if fixed and fixed.is_file():
                hint = (f" Похоже, нужный файл — «{fixed}» (существует): "
                        f"используйте его как --file.")
        raise SystemExit(f"Файл не найден: {path}{hint}")
    if path.suffix.lower() not in OFFICE_EXTS:
        fixed = _corrected_office_name(path)
        if fixed:
            exists = f" (такой файл существует)" if fixed.is_file() else ""
            raise SystemExit(
                f"Неподдерживаемый формат файла «{path.name}»: команды "
                "office_ops работают только с .docx и .xlsx. Похоже, имя "
                f"файла результата перепутано: правильно «{fixed.name}»"
                f"{exists}, а не «{path.name}».")
        raise SystemExit(
            f"Неподдерживаемый формат файла «{path.name}»: команды "
            "office_ops работают только с .docx и .xlsx. Имя файла "
            "результата должно быть вида <имя>.result.<расширение>, "
            "например «Журнал.result.xlsx».")
    return path, path.read_bytes()


def _parse(path: Path, content: bytes):
    return asyncio.run(_parser.parse(content, path.name))


def _assemble(path: Path, content: bytes, edited_text: str, structure: dict) -> bytes:
    """Собрать файл с точечными заменами текста (только изменённые сегменты)."""
    return asyncio.run(_assembler.assemble(
        content, path.name, edited_text, structure,
        strip_hf_images=False, scrub_metadata=False,
    ))


def _is_anonymized_copy(path: Path) -> bool:
    """True для анонимизированной копии <name>.anonymized.<ext> (исходник)."""
    return path.stem.lower().endswith(".anonymized")


def _write_output(args, src: Path, data: bytes) -> Path:
    if getattr(args, "in_place", False):
        out = src
    elif getattr(args, "output", None):
        out = Path(args.output)
    else:
        raise SystemExit("Укажите --output ПУТЬ или --in-place")
    # Правило исходника: анонимизированная копия <name>.anonymized.<ext> —
    # только для чтения, писать в неё нельзя ни при каких условиях.
    if _is_anonymized_copy(out):
        result_hint = out.with_name(
            out.stem.replace(".anonymized", ".result") + out.suffix)
        raise SystemExit(
            f"ЗАПРЕЩЕНО записывать в анонимизированную копию ({out.name}): "
            "она — неизменяемый исходник. Пишите в файл результата: "
            f"--output {result_hint.name}. Если файл результата уже "
            "существует — продолжайте цепочку правок в нём: --file "
            f"{result_hint.name} --output {result_hint.name} (или --in-place).")
    # Имя файла результата обязано иметь расширение Office-документа:
    # «ЖКП….xlsx.result» и тому подобные имена не читаются последующими
    # командами и ломают цепочку правок (дамп/де-анонимизация их не найдут).
    if not getattr(args, "in_place", False) and out.suffix.lower() not in OFFICE_EXTS:
        corrected = _corrected_office_name(out)
        extra = (f" Возможно, вы имели в виду «{corrected.name}»."
                 if corrected else "")
        raise SystemExit(
            f"Неверное имя файла результата: «{out.name}» — расширение "
            f"должно быть .docx или .xlsx.{extra} Для исходника "
            f"«{src.name}» файл результата называется "
            f"«{_result_name_for(src)}».")
    # Защита цепочки правок: команда, начатая заново с анонимизированной
    # копии при существующем файле результата, затрёт предыдущие правки.
    if _is_anonymized_copy(src) and out.is_file():
        print(
            "ВНИМАНИЕ: исходник — анонимизированная копия, а файл результата "
            f"({out.name}) уже существует. Эта команда НАЧНЁТ ЦЕПОЧКУ ЗАНОВО "
            "и затрёт предыдущие правки. Если нужно ПРОДОЛЖИТЬ правки — "
            f"используйте --file {out.name}.",
            file=sys.stderr)
    if not getattr(args, "in_place", False) and out.resolve() == src.resolve():
        print(
            "ВНИМАНИЕ: --output совпадает с исходным файлом. Записи "
            "выполняются атомарно, но команды должны идти СТРОГО "
            "последовательно: параллельный запуск затирает изменения.",
            file=sys.stderr)
    out.parent.mkdir(parents=True, exist_ok=True)
    # Атомарная запись: параллельные/прерванные запуски не оставят
    # «половинный» файл, а читатель увидит либо старую, либо новую версию
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, out)
    return out


def _check_docx(path: Path) -> None:
    if path.suffix.lower() != ".docx":
        raise SystemExit("Команда применима только к файлам .docx")


def _check_xlsx(path: Path) -> None:
    if path.suffix.lower() != ".xlsx":
        raise SystemExit("Команда применима только к файлам .xlsx")


def _set_cell_text(cell, text: str) -> None:
    """Записать (возможно многострочный) текст в ячейку таблицы DOCX."""
    lines = text.split("\n")
    paras = cell.paragraphs
    paras[0].text = lines[0]
    for p in paras[1:]:
        p.text = ""
    for line in lines[1:]:
        cell.add_paragraph(line)


def _one_line(text: str) -> str:
    """Многострочный текст ячейки/абзаца -> одна строка (для вывода)."""
    return re.sub(r"\s*\n+\s*", " / ", text or "").strip()


# ==================== Команды чтения ====================

def _norm_find(s: str) -> str:
    """Нормализация для поиска по dump: нижний регистр, ё→е."""
    return s.lower().replace("ё", "е")


def _filter_dump(text: str, needle: str) -> str:
    """Оставить только строки dump, содержащие needle (регистр и ё/е
    не важны). Надёжный поиск по документу вместо Select-String/grep
    по выводу (который в Cline может не захватиться) и python-однострочников
    с кириллицей (в Windows PowerShell аргументы искажаются)."""
    n = _norm_find(needle)
    kept = [l for l in text.splitlines() if n in _norm_find(l)]
    if not kept:
        raise SystemExit(
            f"Подстрока не найдена в dump: {needle!r}. Проверьте написание "
            "(или уберите --find, чтобы увидеть весь текст документа).")
    return "\n".join(kept)


def cmd_dump(args) -> None:
    """Показать текст документа: сегменты (для apply) или markdown.

    --find "текст" — вывести только строки, содержащие подстроку
    (регистр и ё/е не важны): поиск якоря перед insert-text без
    Select-String/grep и python-однострочников.
    --out-file ПУТЬ — записать результат в файл в UTF-8 средствами
    Python: в отличие от перенаправления «>» (Windows PowerShell
    создаёт UTF-16LE) файл гарантированно читается как UTF-8.
    """
    path, content = _read(args.file)
    parsed = _parse(path, content)
    text = parsed.markdown if args.format == "md" else parsed.text
    find = getattr(args, "find", None)
    if find:
        text = _filter_dump(text, find)
    out_file = getattr(args, "out_file", None)
    if out_file:
        out_path = Path(out_file)
        out_path.write_text(text, encoding="utf-8")
        print(f"OK: dump записан ({len(text)} символов, UTF-8): {out_path}")
    else:
        print(text)


def cmd_list_tables(args) -> None:
    """Обзор структуры: таблицы DOCX или листы XLSX."""
    path, content = _read(args.file)
    if path.suffix.lower() == ".docx":
        doc = Document(io.BytesIO(content))
        if not doc.tables:
            print("Таблиц нет.")
            return
        for i, table in enumerate(doc.tables):
            header = " | ".join(
                c.text.strip().replace("\n", " ") for c in table.rows[0].cells
            ) if table.rows else ""
            print(f"Таблица {i}: {len(table.rows)} строк x "
                  f"{len(table.columns)} столбцов; 1-я строка: {header}")
    elif path.suffix.lower() == ".xlsx":
        wb = load_workbook(io.BytesIO(content))
        for name in wb.sheetnames:
            ws = wb[name]
            print(f"Лист «{name}»: {ws.max_row} строк x {ws.max_column} столбцов")
    else:
        raise SystemExit("Поддерживаются только .docx и .xlsx")


def _resolve_xlsx_column(
        ws, column: str, header_row: int | None = None,
) -> tuple[int, int | None, str]:
    """Определить столбец XLSX: буква (I), номер (9) или текст заголовка.

    Реальные журналы часто имеют шапку не в первой строке, поэтому текст
    заголовка ищется в первых 20 строках листа (или в указанной
    --header-row). Сначала точное совпадение, затем подстрока.
    Возвращает (индекс столбца 1-based, строка заголовка, текст заголовка).
    """
    s = str(column).strip()
    if re.fullmatch(r"[A-Za-z]{1,3}", s):
        col = column_index_from_string(s.upper())
        return col, None, str(ws.cell(row=1, column=col).value or "").strip()
    if re.fullmatch(r"\d{1,3}", s):
        col = int(s)
        if not 1 <= col <= ws.max_column:
            raise SystemExit(
                f"Столбца {col} нет на листе «{ws.title}» "
                f"(столбцов: {ws.max_column})")
        return col, None, str(ws.cell(row=1, column=col).value or "").strip()
    # Текст заголовка
    last_row = header_row if header_row else min(ws.max_row or 1, 20)
    exact = partial = None  # (col, row, text): точное и частичное совпадение
    for r in range(1, last_row + 1):
        for cell in ws[r]:
            text = _fmt_cell_value(cell.value).strip()
            if not text:
                continue
            if text == s:
                exact = (cell.column, r, text)
                break
            if s.lower() in text.lower() and partial is None:
                partial = (cell.column, r, text)
        if exact:
            break
    found = exact or partial
    if not found:
        raise SystemExit(
            f"Заголовок «{s}» не найден в первых {last_row} строках листа "
            f"«{ws.title}». Задайте столбец буквой (I) или номером (9), "
            "либо уточните --header-row.")
    return found


def cmd_read_column(args) -> None:
    """Прочитать значения одной колонки с адресами ячеек.

    XLSX: --column — буква/номер столбца или текст заголовка. Каждое
    значение выводится отдельной строкой с адресом ячейки — колонка
    читается однозначно даже при пустых ячейках и длинных строках
    (плоский dump теряет привязку «значение -> столбец»). Даты — ISO.
    DOCX: --table N --column C — номер столбца (0-based) ИЛИ текст
    заголовка (ищется в первых 10 строках таблицы).
    """
    path, content = _read(args.file)
    if path.suffix.lower() == ".xlsx":
        wb = load_workbook(io.BytesIO(content), data_only=True)
        ws = wb[args.sheet] if args.sheet else wb.active
        col, hdr_row, hdr_text = _resolve_xlsx_column(
            ws, args.column, getattr(args, "header_row", None))
        letter = get_column_letter(col)
        title = (f"Лист «{ws.title}», колонка {letter}"
                 f" ({hdr_text or 'без заголовка'})")
        if hdr_row:
            title += f", заголовок в строке {hdr_row}"
        print(title)
        first_data_row = (hdr_row or 0) + 1
        shown = 0
        for row in range(first_data_row, ws.max_row + 1):
            value = ws.cell(row=row, column=col).value
            if value is None or (isinstance(value, str) and not value.strip()):
                continue
            print(f"{letter}{row} (строка {row}): {_fmt_cell_value(value)}")
            shown += 1
        if not shown:
            print("(значений нет)")
    elif path.suffix.lower() == ".docx":
        doc = Document(io.BytesIO(content))
        try:
            table = doc.tables[int(args.table)]
        except (ValueError, IndexError):
            raise SystemExit("--table: индекс таблицы DOCX (0-based)")
        col = _resolve_docx_column(table, args.column)
        hdr = table.rows[0].cells[col].text.strip() if table.rows else ""
        title = f"Таблица {args.table}, колонка {col}"
        if hdr:
            title += f" ({_one_line(hdr)})"
        print(title)
        for r, row in enumerate(table.rows):
            try:
                text = row.cells[col].text.strip()
            except Exception:  # noqa: BLE001 — слияния ячеек не роняют вывод
                continue
            if text:
                print(f"строка {r}: {_one_line(text)}")
        if not table.rows:
            print("(строк нет)")
    else:
        raise SystemExit("Поддерживаются только .docx и .xlsx")


# ==================== Точечные замены (через FileAssembler) ====================

def cmd_replace_text(args) -> None:
    """Заменить все вхождения подстроки в тексте документа (абзацы, ячейки,
    колонтитулы, фигуры) с сохранением форматирования."""
    path, content = _read(args.file)
    parsed = _parse(path, content)
    new_lines, total = [], 0
    for line in parsed.text.split("\n"):
        n = line.count(args.find)
        total += n
        new_lines.append(line.replace(args.find, args.replace) if n else line)
    if not total:
        raise SystemExit(f"Текст не найден: {args.find!r}")
    out_bytes = _assemble(path, content, "\n".join(new_lines), parsed.structure)
    out = _write_output(args, path, out_bytes)
    print(f"OK: заменено вхождений: {total}; файл: {out}")


def cmd_apply(args) -> None:
    """Применить отредактированный dump (text) обратно к документу.

    Строки файла --from-text должны соответствовать сегментам dump 1:1 —
    правьте текст dump-а, не добавляя и не удаляя строки.
    """
    path, content = _read(args.file)
    parsed = _parse(path, content)
    edited = Path(args.from_text).read_text(encoding="utf-8")
    seg_count = len(parsed.structure.get("segments", []))
    line_count = len(edited.split("\n"))
    if line_count != seg_count:
        extra_hint = ""
        if line_count > seg_count:
            extra_hint = (
                " Похоже, вы ДОБАВИЛИ новые строки в edit-файл: apply "
                "вставку текста не поддерживает — для добавления абзацев "
                "используйте insert-text (без --anchor — в конец документа)."
            )
        raise SystemExit(
            f"Число строк в edit-файле ({line_count}) не совпадает с числом "
            f"сегментов документа ({seg_count}). Правьте вывод dump, не "
            f"добавляя и не удаляя строки.{extra_hint}"
        )
    out_bytes = _assemble(path, content, edited, parsed.structure)
    out = _write_output(args, path, out_bytes)
    print(f"OK: применён {args.from_text}; файл: {out}")



def cmd_insert_text(args) -> None:
    """Вставить новые абзацы (DOCX) или строки (XLSX) в произвольное место.

    DOCX: --anchor — подстрока-ориентир (первый обычный абзац, содержащий
    её; --occurrence N — N-е вхождение, по умолчанию первое); --position
    before|after — вставить до/после ориентира; без
    --anchor текст добавляется в конец документа. Новые абзацы наследуют
    стиль абзаца-ориентира. Каждый --text — абзац; переносы строк внутри
    --text (настоящие и литеральные «\\n») делят его на отдельные абзацы.
    --text-file PATH — абзацы из файла UTF-8 (строка = абзац, пустые
    пропускаются); можно совмещать с --text. Для списка из многих пунктов
    предпочтителен --text-file: ОДНА команда вместо серии insert-text
    (серия хрупка и при параллельном запуске затирает правки).
    XLSX: каждая строка --text/--text-file становится строкой листа
    (столбец A); --row N (1-based) — вставить ПЕРЕД строкой N, без --row —
    в конец листа.
    """
    path, content = _read(args.file)
    if path.suffix.lower() == ".docx":
        _insert_text_docx(args, path, content)
    elif path.suffix.lower() == ".xlsx":
        _insert_text_xlsx(args, path, content)
    else:
        raise SystemExit("Поддерживаются только .docx и .xlsx")


def _split_text_lines(text: str) -> list[str]:
    """Абзацы из одного значения --text: настоящие переносы строк И
    литеральные «\\n» делят текст на отдельные абзацы. Литеральные «\\n»
    часты на практике: PowerShell не интерпретирует \\n в двойных кавычках,
    и модельная строка «====\\nРЕЗЮМЕ\\n====» попадает в аргумент как текст.
    Без деления весь многострочный текст склеивается в один абзац с
    видимыми «\\n». Пустые абзацы отбрасываются."""
    text = (text or "").replace("\\n", "\n")
    parts = [p.strip() for p in text.split("\n")]
    return [p for p in parts if p]


def _collect_insert_lines(args) -> list[str]:
    """Собрать абзацы из --text и/или --text-file.

    --text-file (UTF-8, один абзац на строку; пустые строки пропускаются)
    закрывает кейс «вставить список из N пунктов одной командой»: серия
    из N insert-text с якорем=предыдущему пункту хрупка (каждая — полный
    парс+сохранение docx, один сбой посреди цепочки оставляет документ
    наполовину заполненным), а параллельный батч из двух insert-text
    в Cline затирает правки друг друга.
    """
    lines: list[str] = []
    for t in (args.text or []):
        lines.extend(_split_text_lines(t))
    text_file = getattr(args, "text_file", None)
    if text_file:
        tf_path = Path(text_file)
        if not tf_path.is_file():
            raise SystemExit(
                f"--text-file: файл не найден: {tf_path}. Указывайте путь "
                "внутри папки проекта (файл создаётся редактором заранее).")
        tf_text = tf_path.read_text(encoding="utf-8-sig")
        lines.extend(
            l.strip() for l in tf_text.splitlines() if l.strip())
    if not lines:
        raise SystemExit("Пустой --text/--text-file: нечего вставлять")
    return lines


def _insert_text_docx(args, path: Path, content: bytes) -> None:
    """DOCX: вставить абзацы до/после абзаца-ориентира или в конец."""
    doc = Document(io.BytesIO(content))
    lines = _collect_insert_lines(args)
    if not lines:
        raise SystemExit("Пустой --text: нечего вставлять")
    if args.anchor:
        matches = [p for p in doc.paragraphs if args.anchor in p.text]
        if not matches:
            raise SystemExit(
                f"Абзац-ориентир не найден: {args.anchor!r}. Скопируйте "
                "подстроку из вывода dump (обычные абзацы документа — "
                "ячейки таблиц и колонтитулы якорем быть не могут).")
        occ = getattr(args, "occurrence", None)
        if occ is None:
            occ = 1
        if occ < 1:
            raise SystemExit("--occurrence: номер вхождения указывается с 1")
        if occ > len(matches):
            raise SystemExit(
                f"--occurrence {occ}: якорь {args.anchor!r} встречается "
                f"только в {len(matches)} абзацах")
        anchor = matches[occ - 1]
        occ_note = f", вхождение {occ}" if occ > 1 else ""
        if args.position == "before":
            for line in lines:
                new_p = anchor.insert_paragraph_before(line)
                new_p.style = anchor.style
            where = f"до абзаца-ориентира ({args.anchor[:40]!r}…{occ_note})"
        else:
            ref = anchor._p
            for line in lines:
                new_p = doc.add_paragraph(line)
                new_p.style = anchor.style
                ref.addnext(new_p._p)
                ref = new_p._p
            where = f"после абзаца-ориентира ({args.anchor[:40]!r}…{occ_note})"
    else:
        for line in lines:
            doc.add_paragraph(line)
        where = "в конец документа"
    buf = io.BytesIO()
    doc.save(buf)
    out = _write_output(args, path, buf.getvalue())
    print(f"OK: вставлено абзацев: {len(lines)} ({where}); файл: {out}")


def _insert_text_xlsx(args, path: Path, content: bytes) -> None:
    """XLSX: вставить строки (столбец A) по номеру строки или в конец."""
    wb = load_workbook(io.BytesIO(content))
    ws = wb[args.sheet] if args.sheet else wb.active
    lines = _collect_insert_lines(args)
    if not lines:
        raise SystemExit("Пустой --text: нечего вставлять")
    if args.row is not None:
        if args.row < 1:
            raise SystemExit("--row: номер строки указывается с 1")
        ws.insert_rows(args.row, amount=len(lines))
        for i, line in enumerate(lines):
            ws.cell(row=args.row + i, column=1, value=line)
        where = f"перед строкой {args.row}"
    else:
        for line in lines:
            ws.append([line])
        where = f"в конец листа «{ws.title}»"
    buf = io.BytesIO()
    wb.save(buf)
    out = _write_output(args, path, buf.getvalue())
    print(f"OK: вставлено строк: {len(lines)} ({where}); файл: {out}")

# ==================== Правка таблиц DOCX ====================

def cmd_set_cell(args) -> None:
    """Записать текст в ячейку таблицы по индексам (0-based)."""
    path, content = _read(args.file)
    _check_docx(path)
    doc = Document(io.BytesIO(content))
    try:
        cell = doc.tables[args.table].rows[args.row].cells[args.col]
    except IndexError:
        raise SystemExit("Индексы вне диапазона — сначала list-tables")
    _set_cell_text(cell, args.text)
    buf = io.BytesIO()
    doc.save(buf)
    out = _write_output(args, path, buf.getvalue())
    print(f"OK: таблица {args.table}, ячейка [{args.row}][{args.col}] "
          f"записана; файл: {out}")


def cmd_add_row(args) -> None:
    """Добавить строку в таблицу (в конец или по индексу, с копированием
    форматирования последней строки)."""
    path, content = _read(args.file)
    _check_docx(path)
    doc = Document(io.BytesIO(content))
    table = doc.tables[args.table]
    if args.position in (None, "end"):
        table.add_row()
        row = table.rows[-1]
        position = len(table.rows) - 1
    else:
        position = int(args.position)
        new_tr = deepcopy(table.rows[-1]._tr)
        table.rows[position]._tr.addprevious(new_tr)
        row = table.rows[position]
    values = list(args.cell or [])
    for i, cell in enumerate(row.cells):
        _set_cell_text(cell, values[i] if i < len(values) else "")
    buf = io.BytesIO()
    doc.save(buf)
    out = _write_output(args, path, buf.getvalue())
    print(f"OK: строка добавлена на позицию {position} "
          f"(ячеек заполнено: {min(len(values), len(row.cells))}); файл: {out}")


def _add_column_to_table(table, header: str, values: list[str]) -> None:
    """Добавить столбец справа к одной таблице (заголовок + значения).

    У копируемого оформления ячейки убираются vMerge/gridSpan, чтобы новая
    колонка не «склеивалась» с ячейками строк, где исходная таблица
    использует горизонтальные/вертикальные слияния (копируется ширина и
    границы соседней ячейки).
    """
    # заголовок идёт в первую строку, --cell — по остальным строкам
    values = [header] + list(values or [])
    for row in table.rows:
        new_tc = OxmlElement("w:tc")
        if row.cells:
            tc_pr = row.cells[-1]._element.find(qn("w:tcPr"))
            if tc_pr is not None:
                tc_pr = deepcopy(tc_pr)
                for tag in ("w:vMerge", "w:gridSpan"):
                    el = tc_pr.find(qn(tag))
                    if el is not None:
                        tc_pr.remove(el)
                new_tc.insert(0, tc_pr)
        new_tc.append(OxmlElement("w:p"))
        row._element.append(new_tc)
    grid = table._tbl.find(qn("w:tblGrid"))
    if grid is not None:
        if len(grid):
            grid.append(deepcopy(grid[-1]))
        else:
            grid_col = OxmlElement("w:gridCol")
            grid_col.set(qn("w:w"), "2000")
            grid.append(grid_col)
    for r, row in enumerate(table.rows):
        try:
            cell = row.cells[-1]
        except Exception:  # noqa: BLE001 — экзотические слияния не должны
            continue       # ронять всю операцию ради одной строки
        value = values[r] if r < len(values) else ""
        _set_cell_text(cell, value)


def _new_docx_tc(neighbor_cell) -> "OxmlElement":
    """Новая ячейка (w:tc) с оформлением соседней: из tcPr копируется
    границы соседней ячейки, но vMerge/gridSpan убираются, чтобы новая
    колонка не «склеивалась» со слияниями исходной таблицы."""
    new_tc = OxmlElement("w:tc")
    if neighbor_cell is not None:
        tc_pr = neighbor_cell._element.find(qn("w:tcPr"))
        if tc_pr is not None:
            tc_pr = deepcopy(tc_pr)
            for tag in ("w:vMerge", "w:gridSpan"):
                el = tc_pr.find(qn(tag))
                if el is not None:
                    tc_pr.remove(el)
            new_tc.insert(0, tc_pr)
    new_tc.append(OxmlElement("w:p"))
    return new_tc


def _resolve_docx_column(table, column: str) -> int:
    """Столбец таблицы DOCX (0-based): номер или текст заголовка.
    Заголовок ищется точным совпадением в первых 10 строках, затем
    подстрокой. Возвращает индекс столбца сетки."""
    s = str(column).strip()
    if s.isdigit():
        idx = int(s)
        if not table.rows or idx >= len(table.rows[0].cells):
            raise SystemExit(
                f"Столбца {idx} нет в таблице (столбцов: "
                f"{len(table.rows[0].cells) if table.rows else 0})")
        return idx
    exact = partial = None
    for r, row in enumerate(table.rows[:10]):
        for c, cell in enumerate(row.cells):
            text = cell.text.strip()
            if not text:
                continue
            if text == s:
                exact = c
                break
            if s.lower() in text.lower() and partial is None:
                partial = c
        if exact is not None:
            break
    found = exact if exact is not None else partial
    if found is None:
        header = "; ".join(
            cell.text.strip() for cell in table.rows[0].cells)[:200]
        raise SystemExit(
            f"Заголовок «{s}» не найден в первых 10 строках таблицы. "
            f"Заголовки таблицы: {header}. Задайте столбец номером "
            "(0-based).")
    return found


def _insert_column_into_table(table, insert_idx: int,
                              header: str, values: list[str]) -> None:
    """Вставить столбец DOCX на позицию insert_idx (0-based, сетка):
    в каждую строку — новая ячейка с оформлением соседа, в tblGrid —
    новая gridCol. Значения: строка 0 — header, далее --cell по строкам."""
    grid = table._tbl.find(qn("w:tblGrid"))
    if grid is not None:
        grid_cols = grid.findall(qn("w:gridCol"))
        if grid_cols:
            src = grid_cols[min(insert_idx, len(grid_cols) - 1)]
            new_col = deepcopy(src)
            if insert_idx < len(grid_cols):
                grid_cols[insert_idx].addprevious(new_col)
            else:
                src.addnext(new_col)
        else:
            new_col = OxmlElement("w:gridCol")
            new_col.set(qn("w:w"), "2000")
            grid.append(new_col)
    # значения: строка 0 — заголовок, далее --cell
    fill = [header] + list(values or [])
    for r, row in enumerate(table.rows):
        cells = row.cells
        neighbor = cells[min(insert_idx, len(cells) - 1)] if cells else None
        new_tc = _new_docx_tc(neighbor)
        if insert_idx < len(cells):
            cells[insert_idx]._tc.addprevious(new_tc)
        else:
            row._element.append(new_tc)
        text = fill[r] if r < len(fill) else ""
        if text:
            _set_cell_text(row.cells[insert_idx], text)


def cmd_add_column(args) -> None:
    """Добавить/вставить столбец.

    DOCX: --table N --header "..." [--cell "..."] — столбец в конец
    таблицы; с --column <номер|текст заголовка> и --position before|after
    (по умолчанию after) — вставка на нужное место: справа/слева от
    столбца-ориентира. Новая ячейка наследует оформление соседней.
    XLSX: --column <буква|номер|текст заголовка> + --position или без
    --column — в конец листа. Значения --cell: DOCX — по строкам начиная
    со второй (строка 0 — заголовок), XLSX — со строки после строки
    заголовка; ISO-даты (YYYY-MM-DD) записываются настоящими датами.
    """
    path, content = _read(args.file)
    if path.suffix.lower() == ".xlsx":
        if getattr(args, "table", None):
            raise SystemExit(
                "Параметр --table применяется только к таблицам DOCX. Для "
                "XLSX укажите столбец-ориентир --column (буква, номер или "
                "текст заголовка) с --position before|after и лист через "
                "--sheet; без --column столбец добавляется в конец листа.")
        wb = load_workbook(io.BytesIO(content))
        ws = wb[args.sheet] if args.sheet else wb.active
        values = [_coerce_value(v, getattr(args, "as_text", False))
                  for v in (args.cell or [])]
        insert_at, hdr_row = _xlsx_insert_point(ws, args)
        _adjust_merged_after_col_insert(ws, insert_at)
        ws.insert_cols(insert_at)
        if args.header:
            ws.cell(row=hdr_row, column=insert_at, value=args.header)
        first_value_row = hdr_row + 1 if args.header else hdr_row
        for i, value in enumerate(values):
            cell = ws.cell(row=first_value_row + i, column=insert_at)
            cell.value = value
            if isinstance(value, (datetime, date)):
                cell.number_format = "YYYY-MM-DD"
        buf = io.BytesIO()
        wb.save(buf)
        out = _write_output(args, path, buf.getvalue())
        print(f"OK: столбец {get_column_letter(insert_at)} "
              f"«{args.header or 'без заголовка'}» вставлен на лист "
              f"«{ws.title}» (строка заголовка: {hdr_row}, "
              f"значений: {len(values)}); файл: {out}")
        return
    _check_docx(path)
    doc = Document(io.BytesIO(content))
    if not getattr(args, "table", None):
        raise SystemExit("DOCX: укажите --table N или --table all")
    docx_position = getattr(args, "position", None)
    docx_column = getattr(args, "column", None)
    if args.table.strip().lower() == "all":
        if docx_column or (docx_position and docx_position != "after"):
            raise SystemExit(
                "--table all добавляет столбец только в конец таблиц. Для "
                "вставки в середину укажите конкретную таблицу: --table N "
                "с --column <номер|заголовок> и --position before|after.")
        if not doc.tables:
            raise SystemExit("В документе нет таблиц")
        for table in doc.tables:
            _add_column_to_table(table, args.header, [])
        buf = io.BytesIO()
        doc.save(buf)
        out = _write_output(args, path, buf.getvalue())
        print(f"OK: столбец «{args.header}» добавлен во все "
              f"{len(doc.tables)} таблиц; файл: {out}")
        return
    try:
        table = doc.tables[int(args.table)]
    except (ValueError, IndexError):
        raise SystemExit("--table: индекс таблицы (0-based) или 'all'")
    if docx_column:
        col_idx = _resolve_docx_column(table, docx_column)
        position = docx_position or "after"
        if position not in ("before", "after"):
            raise SystemExit("--position: before|after")
        insert_idx = col_idx + 1 if position == "after" else col_idx
        _insert_column_into_table(table, insert_idx, args.header,
                                  list(args.cell or []))
    else:
        if docx_position and docx_position != "after":
            raise SystemExit(
                "DOCX: --position before|after требует столбец-ориентир "
                "--column <номер|заголовок>. Без --column столбец "
                "добавляется в конец таблицы.")
        _add_column_to_table(table, args.header, list(args.cell or []))
        insert_idx = len(table.columns) - 1
    buf = io.BytesIO()
    doc.save(buf)
    out = _write_output(args, path, buf.getvalue())
    print(f"OK: столбец «{args.header}» "
          f"{'вставлен на позицию ' + str(insert_idx) if docx_column else 'добавлен'} "
          f"({len(table.rows)} строк, столбцов теперь "
          f"{len(table.columns)}); файл: {out}")


# ==================== Правка XLSX ====================

def _coerce_value(value: str, as_text: bool):
    """Числа и даты записывать «настоящими» значениями — иначе Excel видит
    текст и ломает сортировку/формулы; --as-text отключает приведение.
    Даты понимаются в двух видах: ISO «YYYY-MM-DD» и русском «DD.MM.YYYY»
    (остальные варианты остаются текстом)."""
    if as_text or value is None:
        return value
    if _ISO_DATE_RE.fullmatch(value):
        try:
            return datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            return value
    if _DMY_DATE_RE.fullmatch(value):
        try:
            return datetime.strptime(value, "%d.%m.%Y")
        except ValueError:
            return value
    for cast in (int, float):
        try:
            return cast(value)
        except ValueError:
            continue
    return value


_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_DMY_DATE_RE = re.compile(r"\d{1,2}\.\d{1,2}\.\d{4}")


def _guess_header_row(ws) -> int:
    """Наиболее правдоподобная строка заголовков листа (add-column без
    --column): первая из первых 20 строк с >=3 заполненными ячейками.
    Реальные журналы имеют шапку-заголовок не в первой строке — жёсткий
    default «строка 1» кладёт заголовок и значения не туда."""
    for row in range(1, min(ws.max_row or 1, 20) + 1):
        filled = sum(
            1 for cell in ws[row] if cell.value is not None and str(cell.value).strip())
        if filled >= 3:
            return row
    return 1


def _xlsx_insert_point(ws, args) -> tuple[int, int]:
    """Точка вставки столбца XLSX и строка заголовка для add-column.

    --column (буква/номер/текст заголовка) + --position before|after
    (по умолчанию after); без --column — в конец листа, строка заголовка
    определяется автоматически (--guess_header_row) или задаётся
    --header-row.
    """
    if getattr(args, "column", None):
        col, hdr_row, _ = _resolve_xlsx_column(
            ws, args.column, getattr(args, "header_row", None))
        position = getattr(args, "position", "after") or "after"
        if position not in ("before", "after"):
            raise SystemExit("--position: before|after")
        insert_at = col + 1 if position == "after" else col
        return insert_at, hdr_row or _guess_header_row(ws)
    hdr_row = getattr(args, "header_row", None) or _guess_header_row(ws)
    return ws.max_column + 1, hdr_row


def cmd_set_value(args) -> None:
    """Записать значение в ячейку XLSX по адресу (например B2)."""
    path, content = _read(args.file)
    _check_xlsx(path)
    wb = load_workbook(io.BytesIO(content))
    ws = wb[args.sheet] if args.sheet else wb.active
    ws[args.cell.upper()] = _coerce_value(args.value, args.as_text)
    buf = io.BytesIO()
    wb.save(buf)
    out = _write_output(args, path, buf.getvalue())
    print(f"OK: {ws.title}!{args.cell.upper()} = {args.value}; файл: {out}")


def cmd_append_row(args) -> None:
    """Добавить строку в конец листа XLSX (--cell значения по столбцам)."""
    path, content = _read(args.file)
    _check_xlsx(path)
    wb = load_workbook(io.BytesIO(content))
    ws = wb[args.sheet] if args.sheet else wb.active
    ws.append([_coerce_value(v, args.as_text) for v in (args.cell or [])])
    buf = io.BytesIO()
    wb.save(buf)
    out = _write_output(args, path, buf.getvalue())
    print(f"OK: строка добавлена на лист «{ws.title}» "
          f"(значений: {len(args.cell or [])}); файл: {out}")


def _adjust_merged_after_col_delete(ws, col: int) -> None:
    """openpyxl.delete_cols НЕ сдвигает объединённые диапазоны: шапки
    журналов («B1:M1» и т.п.) начинают ссылаться на несуществующий
    столбец, max_column «врёт» (перезагруженный лист показывает лишний
    столбец), list-tables дезинформирует модель. Сдвигаем merge-диапазоны
    вручную: левее удаляемого — на -1, накрывающие его — укорачиваем."""
    saved = [(r.min_row, r.min_col, r.max_row, r.max_col)
             for r in ws.merged_cells.ranges]
    for rng in list(ws.merged_cells.ranges):
        ws.unmerge_cells(str(rng))
    for (r1, c1, r2, c2) in saved:
        if c1 > col:
            c1, c2 = c1 - 1, c2 - 1
        elif c2 >= col:
            c2 -= 1
        if c2 < c1:  # весь merge был в удалённом столбце
            continue
        ws.merge_cells(start_row=r1, start_column=c1, end_row=r2, end_column=c2)


def _adjust_merged_after_col_insert(ws, insert_at: int) -> None:
    """openpyxl.insert_cols, как и delete_cols, НЕ двигает merge-диапазоны:
    шапка-заголовок журнала (merge на всю ширину, «B1:M1») не расширится,
    а merge правее точки вставки останется на прежних индексах. Сдвигаем
    вручную: столбцы правее точки вставки — на +1, накрывающие её —
    расширяем на один столбец."""
    saved = [(r.min_row, r.min_col, r.max_row, r.max_col)
             for r in ws.merged_cells.ranges]
    for rng in list(ws.merged_cells.ranges):
        ws.unmerge_cells(str(rng))
    for (r1, c1, r2, c2) in saved:
        if c1 >= insert_at:
            c1, c2 = c1 + 1, c2 + 1
        elif c2 >= insert_at:
            c2 += 1
        ws.merge_cells(start_row=r1, start_column=c1, end_row=r2, end_column=c2)


def cmd_delete_column(args) -> None:
    """Удалить колонку готовой командой: столбец листа XLSX (по букве,
    номеру или тексту заголовка) либо столбец таблицы DOCX (индекс с 0).

    Альтернатива сырым openpyxl-скриптам, которые теряют форматирование
    и роняют данные журналов (шапка не в первой строке, скрытые столбцы).
    """
    path, content = _read(args.file)
    if path.suffix.lower() == ".xlsx":
        wb = load_workbook(io.BytesIO(content))
        ws = wb[args.sheet] if args.sheet else wb.active
        col, hdr_row, hdr_text = _resolve_xlsx_column(
            ws, args.column, getattr(args, "header_row", None))
        letter = get_column_letter(col)
        _adjust_merged_after_col_delete(ws, col)
        ws.delete_cols(col, 1)
        buf = io.BytesIO()
        wb.save(buf)
        out = _write_output(args, path, buf.getvalue())
        where = f" листа «{ws.title}»" + (f" (заголовок в строке {hdr_row})" if hdr_row else "")
        print(f"OK: удалена колонка {letter} "
              f"({hdr_text or 'без заголовка'}){where}; файл: {out}")
    elif path.suffix.lower() == ".docx":
        doc = Document(io.BytesIO(content))
        try:
            table = doc.tables[int(args.table)]
        except (ValueError, IndexError):
            raise SystemExit("--table: индекс таблицы DOCX (0-based)")
        col = _resolve_docx_column(table, args.column)
        removed_rows = 0
        for row in table.rows:
            tcs = row._tr.findall(qn("w:tc"))
            if col < len(tcs):
                row._tr.remove(tcs[col])
                removed_rows += 1
        grid = table._tbl.find(qn("w:tblGrid"))
        if grid is not None:
            grid_cols = grid.findall(qn("w:gridCol"))
            if col < len(grid_cols):
                grid.remove(grid_cols[col])
        buf = io.BytesIO()
        doc.save(buf)
        out = _write_output(args, path, buf.getvalue())
        print(f"OK: удалён столбец {col} таблицы {args.table} "
              f"({removed_rows} строк); файл: {out}")
    else:
        raise SystemExit("Поддерживаются только .docx и .xlsx")


# ==================== Частичная де-анонимизация (через прокси) ====================

def _env_value(name: str, default: str = "") -> str:
    """Прочитать значение из .env корня проекта."""
    env = Path(__file__).resolve().parent.parent / ".env"
    try:
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return default


# ==================== Разбор аргументов ====================

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="office_ops",
        description="Правка DOCX/XLSX готовыми командами (без python-скриптов). "
                    "Запуск из корня проекта: python -m anonymizer_proxy.office_ops ...",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    out_opts = argparse.ArgumentParser(add_help=False)
    out_opts.add_argument("--output", help="файл результата (исходник не меняется)")
    out_opts.add_argument("--in-place", action="store_true",
                          help="перезаписать исходный файл")

    sp = sub.add_parser("list-tables", help="обзор таблиц DOCX / листов XLSX")
    sp.add_argument("--file", required=True)
    sp.set_defaults(func=cmd_list_tables)

    sp = sub.add_parser("dump", help="вывести текст: сегменты (text) или markdown (md)")
    sp.add_argument("--file", required=True)
    sp.add_argument("--format", choices=["text", "md"], default="text")
    sp.add_argument("--find",
                    help="вывести только строки с этой подстрокой (регистр "
                         "и ё/е не важны) — вместо Select-String/grep")
    sp.add_argument("--out-file",
                    help="записать результат в файл UTF-8 (вместо stdout; "
                         "не используйте «>» — PowerShell пишет UTF-16LE)")
    sp.set_defaults(func=cmd_dump)

    sp = sub.add_parser("read-column",
                        help="значения одной колонки с адресами ячеек "
                             "(XLSX: буква/номер/заголовок; DOCX: --table + "
                             "номер или текст заголовка)")
    sp.add_argument("--file", required=True)
    sp.add_argument("--column", required=True,
                    help="XLSX: буква (I), номер (9) или текст заголовка; "
                         "DOCX: индекс столбца (0-based) или текст заголовка")
    sp.add_argument("--table", help="DOCX: индекс таблицы (0-based)")
    sp.add_argument("--sheet", help="XLSX: имя листа (по умолчанию активный)")
    sp.add_argument("--header-row", type=int,
                    help="XLSX: строка заголовков (по умолчанию поиск в первых 20)")
    sp.set_defaults(func=cmd_read_column)

    sp = sub.add_parser("replace-text", parents=[out_opts],
                        help="замена всех вхождений текста с сохранением форматирования")
    sp.add_argument("--file", required=True)
    sp.add_argument("--find", required=True)
    sp.add_argument("--replace", required=True)
    sp.set_defaults(func=cmd_replace_text)

    sp = sub.add_parser("apply", parents=[out_opts],
                        help="применить отредактированный dump к документу")
    sp.add_argument("--file", required=True)
    sp.add_argument("--from-text", required=True)
    sp.set_defaults(func=cmd_apply)
    sp = sub.add_parser("insert-text", parents=[out_opts],
                        help="вставить новые абзацы/строки в произвольное "
                             "место: DOCX — по якорю или в конец, XLSX — по "
                             "номеру строки или в конец")
    sp.add_argument("--file", required=True)
    sp.add_argument("--anchor",
                    help="подстрока-ориентир в абзаце DOCX (без — в конец)")
    sp.add_argument("--position", choices=("before", "after"), default="after",
                    help="DOCX: вставить до или после абзаца-ориентира")
    sp.add_argument("--occurrence", type=int, default=1,
                    help="DOCX: номер вхождения якоря (с 1; по умолчанию "
                         "первое) — для повторяющихся заголовков")
    sp.add_argument("--sheet", help="XLSX: имя листа (по умолчанию активный)")
    sp.add_argument("--row", type=int,
                    help="XLSX: вставить перед строкой N (с 1); без — в конец")
    sp.add_argument("--text", action="append",
                    help="абзац/строка текста (повторяйте флаг); обязателен "
                         "хотя бы один из --text/--text-file")
    sp.add_argument("--text-file",
                    help="файл UTF-8 с абзацами (строка = абзац) — для "
                         "вставки списка одной командой; можно вместе "
                         "с --text")
    sp.set_defaults(func=cmd_insert_text)

    sp = sub.add_parser("set-cell", parents=[out_opts],
                        help="записать ячейку таблицы DOCX (индексы с 0)")
    sp.add_argument("--file", required=True)
    sp.add_argument("--table", type=int, required=True)
    sp.add_argument("--row", type=int, required=True)
    sp.add_argument("--col", type=int, required=True)
    sp.add_argument("--text", required=True)
    sp.set_defaults(func=cmd_set_cell)

    sp = sub.add_parser("add-row", parents=[out_opts],
                        help="добавить строку таблицы DOCX (--cell повторять)")
    sp.add_argument("--file", required=True)
    sp.add_argument("--table", type=int, required=True)
    sp.add_argument("--cell", action="append", help="значение ячейки новой строки")
    sp.add_argument("--position", default="end", help="end или индекс строки")
    sp.set_defaults(func=cmd_add_row)

    sp = sub.add_parser("add-column", parents=[out_opts],
                        help="добавить столбец: таблице DOCX (--table N/all) "
                             "или в лист XLSX (--column + --position)")
    sp.add_argument("--file", required=True)
    sp.add_argument("--table",
                    help="DOCX: индекс таблицы (0-based) или 'all' — во все таблицы")
    sp.add_argument("--column",
                    help="XLSX: столбец-ориентир — буква (I), номер (9) или "
                         "текст заголовка; без — в конец листа")
    sp.add_argument("--position", choices=["before", "after"], default="after",
                    help="XLSX: вставить до или после столбца-ориентира")
    sp.add_argument("--header", required=True)
    sp.add_argument("--cell", action="append",
                    help="значение по строкам (XLSX: с первой строки после "
                         "строки заголовка); ISO-даты становятся датами")
    sp.add_argument("--sheet", help="XLSX: имя листа (по умолчанию активный)")
    sp.add_argument("--header-row", type=int,
                    help="XLSX: строка заголовков (по умолчанию поиск в первых 20)")
    sp.set_defaults(func=cmd_add_column)

    sp = sub.add_parser("delete-column", parents=[out_opts],
                        help="удалить колонку: столбец листа XLSX "
                             "(буква/номер/заголовок) или столбец таблицы DOCX")
    sp.add_argument("--file", required=True)
    sp.add_argument("--column", required=True,
                    help="XLSX: буква (I), номер (9) или текст заголовка; "
                         "DOCX: индекс столбца (0-based) или текст заголовка")
    sp.add_argument("--table", help="DOCX: индекс таблицы (0-based)")
    sp.add_argument("--sheet", help="XLSX: имя листа (по умолчанию активный)")
    sp.add_argument("--header-row", type=int,
                    help="XLSX: строка заголовков (по умолчанию поиск в первых 20)")
    sp.set_defaults(func=cmd_delete_column)

    sp = sub.add_parser("set-value", parents=[out_opts],
                        help="записать значение в ячейку XLSX")
    sp.add_argument("--file", required=True)
    sp.add_argument("--cell", required=True)
    sp.add_argument("--value", required=True)
    sp.add_argument("--sheet", help="лист (по умолчанию активный)")
    sp.add_argument("--as-text", action="store_true",
                    help="записать как текст, без приведения к числу")
    sp.set_defaults(func=cmd_set_value)

    sp = sub.add_parser("append-row", parents=[out_opts],
                        help="добавить строку в конец листа XLSX")
    sp.add_argument("--file", required=True)
    sp.add_argument("--cell", action="append")
    sp.add_argument("--sheet", help="лист (по умолчанию активный)")
    sp.add_argument("--as-text", action="store_true")
    sp.set_defaults(func=cmd_append_row)

    return parser


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 — CLI не должен ронять трейсбеком
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
