import asyncio, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from anonymizer_proxy.anonymizer.ner_service import NERService

async def test():
    ner = NERService()
    text = "Привет, меня зовут Иван Петров, работаю в ООО Ромашка"
    print(f"Testing NER with text: {text}")
    entities, time_ms = await ner.extract_entities(text, use_llm=True)
    print(f"Entities found: {len(entities)}")
    for e in entities:
        print(f"  {e.type}: \"{e.text}\" [{e.start}:{e.end}]")
    print(f"Time: {time_ms:.0f}ms")
    await ner.close()

asyncio.run(test())