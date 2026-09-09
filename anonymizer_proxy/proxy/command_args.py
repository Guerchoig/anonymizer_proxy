"""
Парсеры аргументов новых чат-команд прокси (v1.11) — детерминированные,
без моделей и сети.

Команды используют канонические формулировки с перечислениями:
- «дополнительно анонимизируй: Иванов, Петрова» — parse_name_list;
- «деанонимизируй плейсхолдеры PERSON_1, PERSON_3–PERSON_5» —
  parse_placeholder_spec (списки и диапазоны, в т.ч. «PERSON_3–5» и
  «с PERSON_3 по PERSON_5»).

Функции чистые: тестируются без сервера (см. tests/test_extra_commands.py).
"""
import re
from typing import List, Tuple

from ..config import PLACEHOLDER_TYPES

# Один плейсхолдер: [PERSON_1] или PERSON_1 (регистр не важен); границы —
# не буква/цифра/подчёркивание, чтобы не матчить внутри других слов
_TOKEN_RE = re.compile(
    r"(?<![A-Za-zА-Яа-яЁё0-9_])\[?(?P<type>[A-Za-z]{2,20})_(?P<num>\d{1,6})\]?"
    r"(?![A-Za-zА-Яа-яЁё0-9])",
    re.IGNORECASE,
)
# Диапазон: PERSON_3–PERSON_5 | PERSON_3..PERSON_5 | PERSON_3-5 |
# с PERSON_3 по PERSON_5 (тип второй границы можно опускать)
_RANGE_RE = re.compile(
    r"(?:\bс\s+)?\[?(?P<t1>[A-Za-z]{2,20})_(?P<n1>\d{1,6})\]?\s*"
    r"(?:[-–—]|\.\.|…|\bпо\b|\bдо\b)\s*"
    r"\[?(?:(?P<t2>[A-Za-z]{2,20})_)?(?P<n2>\d{1,6})\]?",
    re.IGNORECASE,
)
# Разделители перечня имён: запятая, точка с запятой, перевод строки,
# союз «и» (регистрозависимо — чтобы не рвать инициалы «И.»)
_ITEM_SPLIT_RE = re.compile(r"\s*(?:,|;|\n|\s+и\s+)\s*")
# Допустимый элемент списка: ЛЮБАЯ строка, содержащая хотя бы одну
# букву/цифру (имена, номера договоров, даты, суммы — пользователь сам
# решает, что анонимизировать). Классификация «похоже на имя человека» —
# is_name_like() (выбор типа плейсхолдера PERSON/MISC).
_NAME_ITEM_RE = re.compile(r"^[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё.\- ]*$")
_HAS_ALNUM_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9]")
# Упоминания путей/блоков файлов — не имена (вырезаются перед разбором)
_PATH_NOISE_RE = re.compile(
    r"@file:\S+"
    r"|[A-Za-zА-Яа-яЁё0-9_\\/:.\- ]+\.(?:docx|xlsx|xml|txt|md)\b"
    r"|<file_content[^>]*>|</file_content>",
    re.IGNORECASE,
)


def parse_placeholder_spec(text: str) -> Tuple[List[str], List[str]]:
    """Разобрать перечень/диапазоны плейсхолдеров.

    Диапазон разворачивается в отдельные токены и допускается только внутри
    одного типа (PERSON_3–PERSON_5 — да, PERSON_3–ORG_5 — ошибка).

    Returns:
        (tokens, errors): tokens — канонические токены "[ТИП_N]" в порядке
        упоминания без дубликатов; errors — человекочитаемые описания
        нераспознанных элементов (не фатальны, если есть валидные токены).
    """
    tokens: List[str] = []
    errors: List[str] = []
    first_pos: dict = {}

    def _add(etype: str, num: int, pos: int) -> None:
        etype = etype.upper()
        if etype not in PLACEHOLDER_TYPES:
            errors.append(f"{etype}_{num}: неизвестный тип плейсхолдера")
            return
        token = f"[{etype}_{num}]"
        if token not in first_pos or pos < first_pos[token]:
            first_pos[token] = pos

    work = text
    for match in _RANGE_RE.finditer(text):
        t1, n1 = match.group("t1").upper(), int(match.group("n1"))
        t2 = (match.group("t2") or t1).upper()
        n2 = int(match.group("n2"))
        if t1 != t2:
            errors.append(
                f"{t1}_{n1}–{t2}_{n2}: диапазон должен быть внутри одного типа")
        elif n2 < n1:
            errors.append(
                f"{t1}_{n1}–{t2}_{n2}: конец диапазона меньше начала")
        else:
            for num in range(n1, n2 + 1):
                _add(t1, num, match.start())
        # Диапазон уже развёрнут — маскируем, чтобы одиночный токен не
        # сработал повторно на границе диапазона
        work = work.replace(match.group(0), " " * len(match.group(0)), 1)

    for match in _TOKEN_RE.finditer(work):
        _add(match.group("type").upper(), int(match.group("num")),
             match.start())

    tokens = [token for token, _ in sorted(
        first_pos.items(), key=lambda kv: kv[1])]
    return tokens, errors


# Служебные слова между командой и перечнем имён («следующих людей:»)
_FILLER_WORDS = {
    "имена", "имя", "фамилии", "фамилию", "фамилий", "людей", "персоны",
    "следующих", "следующие", "этих", "указанных", "перечисленных",
    "names", "persons",
}


def is_name_like(item: str) -> bool:
    """Похоже ли значение на имя человека (буквы/точки/дефис/пробелы).

    Используется для выбора типа плейсхолдера при дополнительной
    анонимизации: имя → PERSON, прочее (номера, даты, суммы) → MISC.
    """
    return bool(_NAME_ITEM_RE.match(item))


def parse_name_list(segment: str) -> Tuple[List[str], List[str]]:
    """Разобрать перечень значений из текста ПОСЛЕ команды.

    Разделители: запятая, точка с запятой, перевод строки, союз «и»
    (строчно — чтобы не рвать инициалы «И.»). Упоминания путей/блоков
    файлов вырезаются: это не значения. Служебные слова между командой
    и перечнем («следующих людей:») отбрасываются.

    Значением может быть ЛЮБАЯ строка, содержащая хотя бы одну букву/цифру:
    имя человека, номер договора «0095/23/2.1/00075271/013/2023», дату
    «27.12.2023» и т.п. — пользователь сам решает, что анонимизировать
    (багрепорт 2026-09-09: номера/даты ошибочно отвергались как «не имена»).

    Returns:
        (names, errors): names — значения без дубликатов (регистронезависимо);
        errors — нераспознанные элементы (не фатальны, если есть значения).
    """
    cleaned = _PATH_NOISE_RE.sub(" ", segment)
    while True:
        head = cleaned.lstrip(" \t:;—–-")
        first_word = head.split(" ", 1)[0].casefold().strip(",.")
        if first_word in _FILLER_WORDS:
            cleaned = head.split(" ", 1)[1] if " " in head else ""
        else:
            break
    cleaned = cleaned.lstrip(" \t:;—–-")
    names: List[str] = []
    errors: List[str] = []
    seen: set = set()
    for raw_item in _ITEM_SPLIT_RE.split(cleaned):
        item = raw_item.strip(" \t\r\n«»\"'`")
        # Ведущая пунктуация после слова команды («анонимизируй: Иванов»)
        item = item.lstrip(":;—–-").strip(" \t")
        if not item:
            continue
        if (len(item) < 2 or len(item) > 100
                or len(item.split()) > 8
                or not _HAS_ALNUM_RE.search(item)):
            errors.append(item)
            continue
        key = item.casefold()
        if key not in seen:
            seen.add(key)
            names.append(item)
    return names, errors
