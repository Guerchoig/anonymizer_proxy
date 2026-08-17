"""
Функциональные тесты анонимизации локального файла (/api/anonymize_file).

Проверяют создание анонимизированной копии файла (с плейсхолдерами) рядом
с оригиналом, с сохранением структуры (.docx).

Запуск: python anonymizer_proxy\\tests\\test_anonymize_file.py (из корня проекта)
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from anonymizer_proxy.proxy.handlers import RequestHandler
from anonymizer_proxy.tests.test_tools_passthrough import FakeNER, FakeStore, FakeOpenRouter


async def test_anonymize_file_docx():
    from docx import Document

    store = FakeStore()
    handler = RequestHandler(
        ner_service=FakeNER({"Ивана Петрова": "PERSON"}),
        mapping_store=store,
        openrouter_client=FakeOpenRouter(),
    )

    doc = Document()
    doc.add_paragraph("Документ подготовлен от Ивана Петрова")
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "ФИО"
    table.cell(0, 1).text = "Должность"
    table.cell(1, 0).text = "Ивана Петрова"
    table.cell(1, 1).text = "Директор"
    with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tmp:
        doc.save(tmp)
        path = Path(tmp.name)

    anon_path = None
    try:
        result = await handler.handle_anonymize_file(str(path))
        anon_path = Path(result["anonymized_file"])

        assert anon_path.exists(), result
        assert anon_path.name.endswith(".anonymized.docx"), anon_path.name
        assert anon_path.parent == path.parent

        # Копия содержит плейсхолдер, а не оригинальное имя
        d2 = Document(str(anon_path))
        anon_text = "\n".join(p.text for p in d2.paragraphs)
        assert "[PERSON_1]" in anon_text, anon_text
        assert "Ивана Петрова" not in anon_text, anon_text

        # Оригинал не изменён
        d3 = Document(str(path))
        orig_text = "\n".join(p.text for p in d3.paragraphs)
        assert "Ивана Петрова" in orig_text, orig_text

        # Markdown-представление с таблицами: плейсхолдер в markdown-таблице
        md = result["anonymized_markdown"]
        assert "[PERSON_1]" in md, md
        assert "Ивана Петрова" not in md, md
        assert "| ---" in md, md  # markdown-таблица
        assert result["review_file"] is not None

        print("TEST 1 OK: anonymize_file .docx — копия с плейсхолдером, markdown с таблицами")
    finally:
        path.unlink(missing_ok=True)
        if anon_path:
            anon_path.unlink(missing_ok=True)


async def test_anonymize_file_output_path():
    from docx import Document

    store = FakeStore()
    handler = RequestHandler(
        ner_service=FakeNER({"Ивана Петрова": "PERSON"}),
        mapping_store=store,
        openrouter_client=FakeOpenRouter(),
    )
    doc = Document()
    doc.add_paragraph("Текст Ивана Петрова")
    with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tmp:
        doc.save(tmp)
        path = Path(tmp.name)

    out = path.with_name("custom_output.docx")
    try:
        result = await handler.handle_anonymize_file(str(path), output_path=str(out))
        assert Path(result["anonymized_file"]) == out
        assert out.exists()
        print("TEST 2 OK: anonymize_file — явный output_path")
    finally:
        path.unlink(missing_ok=True)
        out.unlink(missing_ok=True)


async def test_anonymize_file_missing():
    store = FakeStore()
    handler = RequestHandler(
        ner_service=FakeNER({}),
        mapping_store=store,
        openrouter_client=FakeOpenRouter(),
    )
    try:
        await handler.handle_anonymize_file("c:/no/such.docx")
        raise AssertionError("ожидался FileNotFoundError")
    except FileNotFoundError:
        pass
    print("TEST 3 OK: anonymize_file — отсутствующий файл -> FileNotFoundError")


async def main():
    await test_anonymize_file_docx()
    await test_anonymize_file_output_path()
    await test_anonymize_file_missing()
    print("\nALL ANONYMIZE FILE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
