"""
Бенчмарк локальной NER-модели (LM Studio): последовательно vs параллельно (2/4).

Замеряет wall-time, prompt/completion токены и размер ответа для чанков
разного размера. Использует тот же системный промпт и формат запроса,
что и продакшен (anonymizer_proxy/anonymizer/ner_service.py).

Запуск (из корня проекта):
    .venv\\Scripts\\python.exe scripts\\benchmark_ner.py [size1,size2,...] [chunks]

Пример:
    .venv\\Scripts\\python.exe scripts\\benchmark_ner.py 10000,30000 4
"""
import asyncio
import random
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from anonymizer_proxy.anonymizer.ner_service import NER_SYSTEM_PROMPT  # noqa: E402
from anonymizer_proxy.config import LM_STUDIO

LM_URL = f"{LM_STUDIO['base_url']}/chat/completions"
MODEL = LM_STUDIO["model"]
TEMPERATURE = LM_STUDIO["temperature"]
MAX_TOKENS = LM_STUDIO["max_tokens"]
TIMEOUT = LM_STUDIO["timeout"]

random.seed(42)

FILLER = [
    "В связи с производственной необходимостью прошу рассмотреть прилагаемые материалы в кратчайшие сроки.",
    "Отчёт подготовлен в соответствии с внутренним регламентом и согласован с руководителем подразделения.",
    "По итогам совещания принято решение оформить документы и направить их на согласование.",
    "Обращаю внимание, что сроки исполнения поручения подходят к концу, требуется подтверждение.",
    "Данные приведены по состоянию на конец отчётного периода и подлежат уточнению.",
    "Просим дать разъяснения по порядку применения указанных положений в текущей работе.",
    "Приложенные сведения носят справочный характер и не являются окончательными.",
    "Рекомендуем ознакомиться с инструкцией перед началом выполнения работ.",
    "Замечания и предложения просьба направлять в установленном порядке.",
    "Контроль исполнения возложен на ответственного сотрудника подразделения.",
]

ENTITIES = [
    "Иванов Иван Иванович",
    "Петрова Анна Сергеевна",
    "Сидоров Пётр Алексеевич",
    "ООО «Ромашка»",
    "АО НПФ Благосостояние",
    "+7 921 123-45-67",
    "ivanov@romashka.ru",
    "ИНН 7701234567",
    "1 500 000 рублей",
    "г. Москва, ул. Ленина, д. 10",
]


def make_text(target_chars: int) -> str:
    """Синтетический русский текст заданной длины с редкими PII-сущностями."""
    parts = []
    length = 0
    i = 0
    while length < target_chars:
        if i % 9 == 0:
            s = f"Сотрудник {random.choice(ENTITIES)} участвует в проекте. "
        else:
            s = random.choice(FILLER) + " "
        parts.append(s)
        length += len(s)
        i += 1
    return "".join(parts)


def payload(text: str) -> dict:
    return {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": NER_SYSTEM_PROMPT},
            {"role": "user", "content": f"Извлеки все сущности из следующего текста:\n\n{text}"},
        ],
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
    }


async def one(client: httpx.AsyncClient, text: str) -> dict:
    t0 = time.perf_counter()
    try:
        r = await client.post(LM_URL, json=payload(text), timeout=TIMEOUT)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "dt": time.perf_counter() - t0, "error": str(e)}
    dt = time.perf_counter() - t0
    try:
        data = r.json()
        msg = data["choices"][0]["message"]
        content = msg.get("content") or ""
        usage = data.get("usage") or {}
        return {
            "ok": r.status_code == 200,
            "dt": dt,
            "status": r.status_code,
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "out_chars": len(content),
            "entities": content.count('"text"'),
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "dt": dt, "error": f"parse: {e}", "status": r.status_code}


def fmt(res: dict) -> str:
    if not res.get("ok"):
        return f"ERR {res.get('status')} {str(res.get('error', ''))[:60]}"
    return (
        f"pt={res['prompt_tokens']} ct={res['completion_tokens']} "
        f"ent={res['entities']} {res['dt']:.1f}s"
    )


async def run_batch(client, texts, concurrency):
    sem = asyncio.Semaphore(concurrency)

    async def worker(text):
        async with sem:
            return await one(client, text)

    t0 = time.perf_counter()
    results = await asyncio.gather(*(worker(t) for t in texts))
    total = time.perf_counter() - t0
    return total, results


async def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    sizes = [int(x) for x in args[0].split(",")] if args else [10000, 30000]
    n_chunks = int(args[1]) if len(args) > 1 else 4

    async with httpx.AsyncClient() as client:
        print("Прогрев (2000 символов)...", flush=True)
        w = await one(client, make_text(2000))
        print("  ->", fmt(w), flush=True)
        if not w.get("ok"):
            print("Модель не отвечает корректно, прерываю.", flush=True)
            return

        print()
        for size in sizes:
            print(f"=== Чанк {size} символов, {n_chunks} шт. ===", flush=True)
            texts = [make_text(size) for _ in range(n_chunks)]

            t_seq, r_seq = await run_batch(client, texts, 1)
            ok = [r for r in r_seq if r.get("ok")]
            avg_seq = sum(r["dt"] for r in ok) / max(1, len(ok))
            print(f"  sequential: total={t_seq:.1f}s  avg_req={avg_seq:.1f}s", flush=True)
            for r in r_seq:
                print("    " + fmt(r), flush=True)

            t2, r2 = await run_batch(client, texts, 2)
            ok2 = [r for r in r2 if r.get("ok")]
            avg2 = sum(r["dt"] for r in ok2) / max(1, len(ok2))
            print(f"  parallel(2): total={t2:.1f}s  avg_req={avg2:.1f}s", flush=True)
            for r in r2:
                print("    " + fmt(r), flush=True)

            t4, r4 = await run_batch(client, texts, 4)
            ok4 = [r for r in r4 if r.get("ok")]
            avg4 = sum(r["dt"] for r in ok4) / max(1, len(ok4))
            print(f"  parallel(4): total={t4:.1f}s  avg_req={avg4:.1f}s", flush=True)
            for r in r4:
                print("    " + fmt(r), flush=True)

            if ok and ok2 and ok4:
                print(
                    f"  => speedup par2={t_seq / max(0.001, t2):.2f}x  "
                    f"par4={t_seq / max(0.001, t4):.2f}x  "
                    f"(prompt_tokens~{ok[0]['prompt_tokens']})", flush=True,
                )
            print(flush=True)


if __name__ == "__main__":
    asyncio.run(main())


