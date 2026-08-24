"""
Проба GLiNER-модели на русском тексте: какие сущности и с какой
уверенностью находит модель, замер времени и пика RAM.

Запуск (из корня проекта):
    .venv\\Scripts\\python.exe scripts\\gliner_probe.py

Опции:
    --file <путь>    прогнать модель по тексту документа (DOCX/XLSX/TXT/MD)
    --labels         показать список используемых меток
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from anonymizer_proxy.anonymizer.gliner_engine import DEFAULT_LABEL_MAP  # noqa: E402
from anonymizer_proxy.config import NER_ENGINE, PII_CATEGORIES  # noqa: E402

SAMPLE = (
    "Генеральному директору ООО «Ромашка» Иванову Ивану Ивановичу. "
    "Главный бухгалтер Петрова Анна Сергеевна, отдел кадров. "
    "Тел.: +7 921 123-45-67, email: ivanov@romashka.ru. "
    "ИНН 7701234567, КПП 770101001, ОГРН 1234567890123. "
    "Сумма договора 1 500 000 рублей. г. Москва, ул. Ленина, д. 10. "
    "Используется система 1С:Предприятие."
)


def main() -> int:
    args = sys.argv[1:]
    file_path = None
    if "--file" in args:
        file_path = args[args.index("--file") + 1]
    if "--labels" in args:
        print("Метки GLiNER для категорий проекта:")
        for cat, label in DEFAULT_LABEL_MAP.items():
            print(f"  {cat:10s} -> {label}")
        return 0

    from gliner import GLiNER

    print(f"Модель: {NER_ENGINE['model']} (device={NER_ENGINE['device']})", flush=True)
    t0 = time.perf_counter()
    model = GLiNER.from_pretrained(NER_ENGINE["model"], map_location=NER_ENGINE["device"])
    load_s = time.perf_counter() - t0
    print(f"Загрузка: {load_s:.1f} с", flush=True)

    text = SAMPLE
    if file_path:
        from anonymizer_proxy.anonymizer.file_parser import FileParser
        parsed = FileParser().parse(Path(file_path))
        text = parsed.text
        print(f"Файл: {file_path} ({len(text)} символов)", flush=True)

    labels = list(DEFAULT_LABEL_MAP.values())
    t0 = time.perf_counter()
    entities = model.predict_entities(
        text=text,
        labels=labels,
        flat_ner=True,
        threshold=NER_ENGINE["threshold"],
    )
    dt = time.perf_counter() - t0
    print(f"Инференс: {dt:.2f} с, найдено {len(entities)} сущностей", flush=True)
    print("-" * 60)
    for e in sorted(entities, key=lambda x: x["start"]):
        print(
            f"{e['label']:30s} {e['score']:.2f}  "
            f"[{e['start']}:{e['end']}]  {e['text']!r}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
