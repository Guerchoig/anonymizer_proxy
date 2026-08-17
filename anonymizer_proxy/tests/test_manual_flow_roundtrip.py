"""
Сквозной тест файлового сценария ручного управления анонимизацией.

Проверяет полный цикл:
1. anonymize_file создаёт анонимизированную копию (с плейсхолдерами и таблицами).
2. deanonymize_file возвращает реальные значения в конце.

Запуск: python anonymizer_proxy\\tests\\test_manual_flow_roundtrip.py (из корня проекта)
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from anonymizer_proxy.proxy.handlers import RequestHandler
from anonymizer_proxy.tests.test_tools_passthrough import FakeNER, FakeStore, FakeOpenRouter


async def test_file_roundtrip_docx_with_table():
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
    restored = None
    try:
        # 1. Анонимизировать файл
        result = await handler.handle_anonymize_file(str(path))
        sid = result["session_id"]
        anon_path = Path(result["anonymized_file"])

        # 2. Де-анонимизировать копию в новый файл (финальный шаг)
        restored = path.with_name("restored.docx")
        await handler.handle_deanonymize_file(sid, str(anon_path), str(restored))

        # 3. Проверить восстановление: значения вернулись, плейсхолдеров нет
        d = Document(str(restored))
        text = "\n".join(p.text for p in d.paragraphs)
        for t in d.tables:
            for row in t.rows:
                for cell in row.cells:
                    text += "\n" + cell.text

        assert "Ивана Петрова" in text, text
        assert "[PERSON_1]" not in text, text
        print("TEST 1 OK: round-trip DOCX с таблицей — анонимизация -> де-анонимизация")
    finally:
        path.unlink(missing_ok=True)
        if anon_path:
            anon_path.unlink(missing_ok=True)
        if restored:
            restored.unlink(missing_ok=True)


async def main():
    await test_file_roundtrip_docx_with_table()
    print("\nALL MANUAL FLOW ROUNDTRIP TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
