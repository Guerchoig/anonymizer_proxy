"""
ИИ-детектор чат-команд прокси на GLiNER (in-process, без llama-server).

Гибридная схема детектирования (модуль — только GLiNER-слой):

1. Дешёвый regex-префильтр (COMMAND_TRIGGER_RE) — решает, стоит ли вообще
   обращаться к модели; обычные промты классификацию не проходят.
2. Детерминированные правила handlers.resolve_chat_command — точные
   формулировки, включая перезапуск (деструктивная команда — ТОЛЬКО правила:
   GLiNER путает «перезапусти прокси» с «перезапусти тестовый сервер»).
3. GLiNER zero-shot (этот модуль) — свободные формулировки «безопасных»
   команд: переключение бэкенда, де-анонимизация. Сообщение сравнивается
   с классами-метками; команда признаётся при скоре выше порога и отрыве
   от второй метки (COMMAND_CLASSIFIER["threshold"/"margin"]).
   ВАЖНО: слой ОТКЛЮЧЁН ПО УМОЛЧАНИЮ (COMMAND_CLASSIFIER=off): детекция
   команд выполняется только детерминированными regex-правилами. Опыт
   показал, что zero-shot классификаторы (GLiNER, NLI mDeBERTa) в роли
   детектора команд нестабильны: скоры не калиброваны и сильно зависят
   от формулировок (проверено живыми тестами). Включить GLiNER-слой:
   COMMAND_CLASSIFIER=auto.
4. Недоступность GLiNER — исключение/None: вызывающий код откатывается
   к правилам, основной поток запроса не ломается.
"""
import logging
from typing import Optional

from ..config import COMMAND_CLASSIFIER
from .utils import COMMAND_TRIGGER_RE

logger = logging.getLogger("anonymizer_proxy.command_classifier")

# Классы-метки GLiNER для «безопасных» команд. Формулировки — русские
# описания: GLiNER матчит спан сообщения с описанием класса (zero-shot).
COMMAND_LABELS: dict[str, str] = {
    "backend_local": "команда переключения на локальную модель",
    "backend_cloud": "команда переключения на облако",
    "deanon_files": "команда деанонимизации файлов целиком",
}
_REVERSE_LABELS = {label: intent for intent, label in COMMAND_LABELS.items()}


def looks_like_command(texts: list[str]) -> bool:
    """Дешёвый префильтр: похоже ли сообщение на чат-команду прокси.

    Отсекает заведомо обычные промты без обращения к GLiNER; ложные
    срабатывания допустимы — их отсеют правила и классификация.
    """
    if not texts:
        return False
    return bool(COMMAND_TRIGGER_RE.search("\n".join(texts)))


async def classify_command_intent(engine, texts: list[str]) -> Optional[str]:
    """
    GLiNER zero-shot: вернуть вид команды или None (не команда / не уверен).

    engine — GlinerEngine (predict_with_labels). Порог и требуемый отрыв
    от второй метки настраиваются в COMMAND_CLASSIFIER (порог 0.05:
    позитивы на реальной модели дают 0.05–0.23, негативы — 0).
    """
    if COMMAND_CLASSIFIER.get("mode", "off") != "auto":
        return None

    text = "\n".join(texts)[: COMMAND_CLASSIFIER["max_chars"]]
    raw = await engine.predict_with_labels(
        text,
        list(COMMAND_LABELS.values()),
        threshold=0.0,  # получаем сырые скоры, порог применяем сами
        timeout=COMMAND_CLASSIFIER["timeout"],
    )
    if not raw:
        return None

    # Лучший скор по каждой метке (GLiNER может вернуть несколько спанов)
    best: dict[str, float] = {}
    for ent in raw:
        intent = _REVERSE_LABELS.get(str(ent.get("label", "")))
        if not intent:
            continue
        score = float(ent.get("score", 0.0))
        if score > best.get(intent, 0.0):
            best[intent] = score
    if not best:
        return None

    ranked = sorted(best.items(), key=lambda kv: kv[1], reverse=True)
    top_intent, top_score = ranked[0]
    if top_score < COMMAND_CLASSIFIER["threshold"]:
        return None
    # При близких скорах двух меток модель не уверена — воздерживаемся
    if len(ranked) > 1:
        runner_score = ranked[1][1]
        if runner_score > 0 and top_score < runner_score * COMMAND_CLASSIFIER["margin"]:
            logger.info(
                "GLiNER-детектор команд: скоры близки (%s %.2f vs %s %.2f) — "
                "команда не распознана",
                top_intent, top_score, ranked[1][0], runner_score,
            )
            return None
    logger.info("GLiNER-детектор команд: %s (скор %.2f)", top_intent, top_score)
    return top_intent