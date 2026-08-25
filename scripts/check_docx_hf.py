"""
Ручная проверка доработки анонимизации DOCX на реальных файлах:
- текст колонтитулов и фигур попадает в сегменты;
- при сборке PII заменён плейсхолдерами, картинки из колонтитулов удалены,
  их media вымыты из пакета;
- структура сегментов обратимой сборки совпадает (важно для де-анонимизации).

Запуск: .\\.venv\\Scripts\\python.exe scripts\\check_docx_hf.py
"""
import asyncio
import io
import re
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from docx import Document
from docx.oxml.ns import qn

from anonymizer_proxy.anonymizer.file_parser import (
    FileParser,
    FileAssembler,
)

DOCS_DIR = Path(__file__).resolve().parent.parent / "docs"
OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "tmp_hf_check"

RULES = [
    ("Информационные Розничные Интегрированные Системы", "[ORG_1]"),
    ("Общество с ограниченной ответственностью", "[ORG_4]"),
    ("614087, г. Пермь, а/я 8", "[LOC_1]"),
    ("101000, г. Москва, Бобров пер., д. 6, стр. 3, пом. I", "[LOC_2]"),
    ("+7 (495) 137-00-99", "[PHONE_1]"),
    ("info@iris-retail.ru", "[EMAIL_1]"),
    ("www.iris-retail.ru", "[LOC_3]"),
    ("Группа компаний ITPS | ООО «Парма-Телеком»", "[ORG_2]"),
    ("ООО «Парма-Телеком»", "[ORG_2]"),
    ("115035, Россия, Москва", "[LOC_4]"),
    ("Овчинниковская наб., 20. стр.1", "[LOC_5]"),
    ("info@itps-russia.ru", "[EMAIL_2]"),
    ("www.itps.com", "[LOC_6]"),
    ("ОГРН 1035900071987", "ОГРН [INN_1]"),
    ("ИНН 5902191377", "ИНН [INN_2]"),
    ("614000, Россия, Пермь", "[LOC_7]"),
    ("ул. Советская, 51а", "[LOC_8]"),
    ("+7 (495) 660 81 81", "[PHONE_2]"),
    ("+7 (342) 206 06 75", "[PHONE_3]"),
    ("АО «НПФ БЛАГОСОСТОЯНИЕ»", "[ORG_3]"),
    ("НПФ «БЛАГОСОСТОЯНИЕ»", "[ORG_3]"),
    ("Милюкову Анатолию Анатольевичу", "[PERSON_1]"),
]

LEAK_MARKERS = [
    "Милюков", "БЛАГОСОСТОЯНИЕ", "iris-retail", "Парма-Телеком",
    "itps-russia", "1035900071987", "5902191377", "137-00-99",
]


def compile_rule(sample: str):
    """Точный шаблон: пробелы совпадают с обычным пробелом и NBSP."""
    pattern = re.escape(sample).replace(r"\ ", r"[ \xa0]+")
    return re.compile(pattern)


async def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fp, fa = FileParser(), FileAssembler()
    rules = [(compile_rule(s), repl) for s, repl in RULES]

    for name in ["KP_IRIS.docx", "КП ДО 01.docx"]:
        src = DOCS_DIR / name
        content = src.read_bytes()
        parsed = await fp.parse(content, name)

        hf_lines = sum(len(e["paras"]) for e in parsed.structure["headers_footers"])
        shape_lines = sum(len(e["paras"]) for e in parsed.structure["textboxes"])
        print(f"\n=== {name}")
        print(f"сегментов всего: {len(parsed.structure['segments'])}, "
              f"в колонтитулах: {hf_lines}, в фигурах: {shape_lines}")
        assert hf_lines > 0, f"{name}: колонтитулы не извлечены"

        for entry in parsed.structure["textboxes"]:
            print(f"  фигура[{entry['index']}] ({entry['host']}): "
                  f"{entry['paras'][:1]}")

        # Имитируем анонимизацию NER: заменяем PII на плейсхолдеры
        anon_text = parsed.text
        for rx, repl in rules:
            anon_text = rx.sub(repl, anon_text)

        out_bytes = await fa.assemble(content, name, anon_text, parsed.structure)
        target = OUT_DIR / f"{src.stem}.anonymized{src.suffix}"
        target.write_bytes(out_bytes)

        # --- Проверки результата ---
        doc = Document(io.BytesIO(out_bytes))
        for hf in doc.sections:
            for holder in (hf.header, hf.footer,
                           hf.first_page_header, hf.first_page_footer,
                           hf.even_page_header, hf.even_page_footer):
                if holder.is_linked_to_previous:
                    continue
                root = holder.part.element
                assert root.find(".//" + qn("w:drawing")) is None, \
                    f"{name}: drawing остался в {holder.part.partname}"
                assert root.find(".//" + qn("w:pict")) is None, \
                    f"{name}: pict остался в {holder.part.partname}"

        with zipfile.ZipFile(io.BytesIO(content)) as z0:
            media_before = [n for n in z0.namelist() if n.startswith("word/media/")]
        all_xml = ""
        with zipfile.ZipFile(io.BytesIO(out_bytes)) as z:
            media_after = [n for n in z.namelist() if n.startswith("word/media/")]
            for n in z.namelist():
                if n.endswith(".xml"):
                    all_xml += z.read(n).decode("utf-8", errors="ignore")
        for marker in LEAK_MARKERS:
            assert marker not in all_xml, f"{name}: утечка '{marker}'"
        print(f"  media: было {len(media_before)} -> стало {len(media_after)}")

        # Структурная консистентность: парсинг результата даёт ту же
        # геометрию сегментов (условие корректной де-анонимизации)
        reparsed = await fp.parse(out_bytes, name)
        kinds_a = [s["kind"] for s in parsed.structure["segments"]]
        kinds_b = [s["kind"] for s in reparsed.structure["segments"]]
        assert len(kinds_a) == len(kinds_b), \
            f"{name}: число сегментов разъехалось {len(kinds_a)} != {len(kinds_b)}"
        mismatches = [(i, a, b) for i, (a, b)
                      in enumerate(zip(kinds_a, kinds_b)) if a != b]
        assert not mismatches, \
            f"{name}: виды сегментов разошлись: {mismatches[:5]}"
        print(f"  OK: сегментов {len(kinds_b)}, расхождений по видам нет")

    print("\nVALIDATION PASSED")


if __name__ == "__main__":
    asyncio.run(main())
