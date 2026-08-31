"""
office_ops — CLI-инструментарий правки документов MS Office без Python-кода.

Облачная модель вызывает готовые команды вместо написания скриптов на
python-docx/openpyxl (запуск из корня проекта):

  python -m anonymizer_proxy.office_ops list-tables --file "doc.docx"
  python -m anonymizer_proxy.office_ops dump --file "doc.docx" [--format md]
  python -m anonymizer_proxy.office_ops replace-text --file F --find "a" --replace "b" --output OUT
  python -m anonymizer_proxy.office_ops set-cell --file F --table 0 --row 1 --col 2 --text "..." --output OUT
  python -m anonymizer_proxy.office_ops add-row --file F --table 0 --cell "a" --cell "b" [--position 1] --output OUT
  python -m anonymizer_proxy.office_ops add-column --file F --table 0 --header "..." [--cell "..."] --output OUT
  python -m anonymizer_proxy.office_ops set-value --file F.xlsx --cell B2 --value 150 [--sheet Лист1] --output OUT
  python -m anonymizer_proxy.office_ops append-row --file F.xlsx [--sheet Лист1] --cell "a" --cell "b" --output OUT
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
import sys
import urllib.error
import urllib.request
from copy import deepcopy
from pathlib import Path

try:
    from .anonymizer.file_parser import FileParser, FileAssembler
except ImportError:  # прямой запуск файла: python anonymizer_proxy\office_ops.py
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from anonymizer_proxy.anonymizer.file_parser import FileParser, FileAssembler

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from openpyxl import load_workbook

_parser = FileParser()
_assembler = FileAssembler()


# ==================== Общие помощники ====================

def _read(path_str: str) -> tuple[Path, bytes]:
    path = Path(path_str)
    if not path.is_file():
        raise SystemExit(f"Файл не найден: {path}")
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


# ==================== Команды чтения ====================

def cmd_dump(args) -> None:
    """Показать текст документа: сегменты (для apply) или markdown."""
    path, content = _read(args.file)
    parsed = _parse(path, content)
    print(parsed.markdown if args.format == "md" else parsed.text)


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
        raise SystemExit(
            f"Число строк в edit-файле ({line_count}) не совпадает с числом "
            f"сегментов документа ({seg_count}). Правьте вывод dump, не "
            f"добавляя и не удаляя строки."
        )
    out_bytes = _assemble(path, content, edited, parsed.structure)
    out = _write_output(args, path, out_bytes)
    print(f"OK: применён {args.from_text}; файл: {out}")



def cmd_insert_text(args) -> None:
    """Вставить новые абзацы (DOCX) или строки (XLSX) в произвольное место.

    DOCX: --anchor — подстрока-ориентир (первый обычный абзац, содержащий
    её); --position before|after — вставить до/после ориентира; без
    --anchor текст добавляется в конец документа. Новые абзацы наследуют
    стиль абзаца-ориентира.
    XLSX: каждая строка --text становится строкой листа (столбец A);
    --row N (1-based) — вставить ПЕРЕД строкой N, без --row — в конец
    листа.
    """
    path, content = _read(args.file)
    if path.suffix.lower() == ".docx":
        _insert_text_docx(args, path, content)
    elif path.suffix.lower() == ".xlsx":
        _insert_text_xlsx(args, path, content)
    else:
        raise SystemExit("Поддерживаются только .docx и .xlsx")


def _insert_text_docx(args, path: Path, content: bytes) -> None:
    """DOCX: вставить абзацы до/после абзаца-ориентира или в конец."""
    doc = Document(io.BytesIO(content))
    lines = list(args.text)
    if args.anchor:
        anchor = None
        for p in doc.paragraphs:
            if args.anchor in p.text:
                anchor = p
                break
        if anchor is None:
            raise SystemExit(
                f"Абзац-ориентир не найден: {args.anchor!r}. Скопируйте "
                "подстроку из вывода dump (обычные абзацы документа — "
                "ячейки таблиц и колонтитулы якорем быть не могут).")
        if args.position == "before":
            for line in lines:
                new_p = anchor.insert_paragraph_before(line)
                new_p.style = anchor.style
            where = f"до абзаца-ориентира ({args.anchor[:40]!r}…)"
        else:
            ref = anchor._p
            for line in lines:
                new_p = doc.add_paragraph(line)
                new_p.style = anchor.style
                ref.addnext(new_p._p)
                ref = new_p._p
            where = f"после абзаца-ориентира ({args.anchor[:40]!r}…)"
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
    lines = list(args.text)
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
    использует горизонтальные/вертикальные слияния.
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


def cmd_add_column(args) -> None:
    """Добавить столбец справа: в одну таблицу (--table N) или во все
    таблицы документа одной командой (--table all)."""
    path, content = _read(args.file)
    _check_docx(path)
    doc = Document(io.BytesIO(content))
    if args.table.strip().lower() == "all":
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
    _add_column_to_table(table, args.header, list(args.cell or []))
    buf = io.BytesIO()
    doc.save(buf)
    out = _write_output(args, path, buf.getvalue())
    print(f"OK: столбец «{args.header}» добавлен "
          f"({len(table.rows)} строк); файл: {out}")


# ==================== Правка XLSX ====================

def _coerce_value(value: str, as_text: bool):
    """Числа записывать числами (иначе Excel видит текст), если не --as-text."""
    if as_text:
        return value
    for cast in (int, float):
        try:
            return cast(value)
        except ValueError:
            continue
    return value


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
    sp.set_defaults(func=cmd_dump)

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
    sp.add_argument("--sheet", help="XLSX: имя листа (по умолчанию активный)")
    sp.add_argument("--row", type=int,
                    help="XLSX: вставить перед строкой N (с 1); без — в конец")
    sp.add_argument("--text", action="append", required=True,
                    help="абзац/строка текста (повторяйте флаг)")
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
                        help="добавить столбец таблице DOCX (--table all — во все)")
    sp.add_argument("--file", required=True)
    sp.add_argument("--table", required=True,
                    help="индекс таблицы (0-based) или 'all' — во все таблицы")
    sp.add_argument("--header", required=True)
    sp.add_argument("--cell", action="append", help="значение по строкам")
    sp.set_defaults(func=cmd_add_column)

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
