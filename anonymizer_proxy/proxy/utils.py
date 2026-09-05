"""
Утилиты прокси: пути файлов, блоки <file_content>, намерения анонимизации.

Вынесено из handlers.py, чтобы уменьшить god-object и дать чистым функциям
(без состояния) переиспользоваться в других модулях и тестах.
"""
import os
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

from ..config import BASE_DIR

# Блоки <file_content path="...">...</file_content>, которые клиент (Cline)
# вставляет в текст сообщения при прикладывании файлов
FILE_CONTENT_BLOCK_RE = re.compile(
    r'<file_content path="(?P<path>[^"]+)">(?P<body>.*?)</file_content>',
    re.DOTALL,
)
# Префикс заглушки-ошибки: клиент не смог прочитать файл (обычно бинарный
# документ вида DOCX/XLSX) и прислал вместо содержимого текст ошибки
FILE_CONTENT_ERROR_PREFIX = "Error fetching content"
# Упоминание Office-файла (.docx/.xlsx) в произвольном тексте. Клиенто-
# независимый триггер шпаргалки office_ops: Cline присылает блоки
# <file_content path="...">, другие агенты (Hermes и т.п.) — пути в тексте
# сообщений и в аргументах tool_calls. Требуем непробельные символы перед
# расширением (имя/путь файла), чтобы не срабатывать на «форматы .docx»
# без конкретного файла.
OFFICE_PATH_RE = re.compile(r"\S+\.(?:docx|xlsx)\b", re.IGNORECASE)
# Ссылки на файлы в стиле Hermes: «@file:<путь>» в тексте сообщения.
# Desktop-клиент Hermes передаёт вложения такими ссылками (без блоков
# <file_content path="...">); путь может быть в кавычках "…"/'…' или в
# бэктиках `…` (Hermes оборачивает в них даже пути с пробелами:
# @file:`AppData/Local/hermes/attachments/ERP.СМ Соглашение.docx`).
ATTACHMENT_FILE_REF_RE = re.compile(
    r"@file:(?P<path>\"[^\"]+\"|'[^']+'|`[^`]+`|[^\s,;)\]]+)",
    re.IGNORECASE,
)
# Путь в бэктиках с офисным расширением — так Hermes печатает блок
# «Attached Context»: доступно на диске в `C:\…\attachments\Имя с пробелами.docx`.
# Отдельная регулярка, потому что путь с пробелами не взять по \S+.
BACKTICKED_OFFICE_PATH_RE = re.compile(
    r"`([^`\r\n]+\.(?:docx|xlsx|xml|txt|md))`",
    re.IGNORECASE,
)
# Упоминание файла поддерживаемого формата простым путём в тексте сообщения
# («анонимизируй файл c:\docs\KP_IRIS.docx») — без @file: и без <file_content>.
# Граница \b отсекает замыкающую пунктуацию («…docx.» / «…docx,»); несуществующие
# пути отфильтровываются при разрешении (файл обязан существовать).
PATH_MENTION_RE = re.compile(
    r"[^\s\"'<>|*?]+\.(?:docx|xlsx|xml|txt|md)\b",
    re.IGNORECASE,
)
# Корень рабочей области для относительных путей из <file_content path="...">
# (BASE_DIR — корень проекта, он же workspace; сервер запускается из него)
WORKSPACE_ROOT = BASE_DIR

# Намерение анонимизации в тексте запроса («Анонимизируй файл…»).
# Триггер автоматической анонимизации приложенных файлов в passthrough-режиме
ANONYMIZE_INTENT_RE = re.compile(r"анонимиз|anonymi[sz]", re.IGNORECASE)
# Маркер «файл уже анонимизирован» в ответе прокси: защита от повторной
# анонимизации при следующих запросах агента (история диалога накапливается,
# исходный <file_content>-блок и команда «анонимизируй» остаются в ней)
ANONYMIZER_DONE_MARKER_RE = re.compile(r"\[anonymizer:done:(?P<path>[^\]]+)\]")
# session_id из ответа перехвата («session_id: <uuid>» внутри блока [ANONYMIZER])
SESSION_ID_LINE_RE = re.compile(r"session_id:\s*([A-Za-z0-9_-]{1,64})")
# Намерение де-анонимизации («деанонимизируй файлы…»). Проверяется ДО
# анонимизации, т.к. слово «деанонимизируй» содержит «анонимизируй».
DEANONYMIZE_INTENT_RE = re.compile(r"де.?анонимиз|deanonymi[sz]", re.IGNORECASE)
# Чат-команды управления прокси (проверяются ТОЛЬКО в текущем сообщении
# пользователя — см. last_user_message_texts): перезапуск сервера и
# переключение активного LLM-бэкенда (OpenRouter / локальная LM Studio).
# «перезагрузи прокси» и «рестартни сервер» — те же команды другими словами.
RESTART_INTENT_RE = re.compile(
    r"(?:перезапусти|перезапустить|перезагрузи|перезагрузить"
    r"|рестартни|рестартан)\w*\s+(?:прокси|сервер)"
    r"|перезапуск\s+(?:прокси|сервера)"
    r"|перезагрузк\w+\s+(?:прокси|сервера)"
    r"|\brestart\s+(?:the\s+)?proxy\b",
    re.IGNORECASE,
)
LOCAL_BACKEND_RE = re.compile(
    r"(?:переключи|переключись|работай|используй|верни)\w*[^\n]{0,40}локальн",
    re.IGNORECASE,
)
CLOUD_BACKEND_RE = re.compile(
    r"(?:переключи|переключись|работай|используй|верни|вернись)\w*[^\n]{0,40}"
    r"(?:обла(?:чн|к)|cloud)",
    re.IGNORECASE,
)
# Машиночитаемый маркер пути анонимизированной копии в ответе перехвата
ANONYMIZER_COPY_MARKER_RE = re.compile(r"\[anonymizer:copy:(?P<path>[^\]]+)\]")
# Маркер пути «файла результата» — куда модель пишет правки, не изменяя копию
ANONYMIZER_RESULT_MARKER_RE = re.compile(r"\[anonymizer:result:(?P<path>[^\]]+)\]")
# Дешёвый префильтр-подозрение на чат-команду прокси (детектирование гибридное:
# правила + ИИ-классификация). Специально НЕ включает одиночное «анонимиз» —
# у авто-анонимизации приложенных файлов свой детерминированный триггер.
# Ложные срабатывания (упоминание «прокси»/«локальн» в обычном тексте) не
# страшны: ИИ-классификатор вернёт none, и запрос пойдёт обычным порядком.
COMMAND_TRIGGER_RE = re.compile(
    r"перезапус|перезагруз|рестарт|\brestart\b"
    r"|прокси|\bproxy\b|бэкенд|бекенд|\bbackend\b"
    r"|локальн|обла(?:чн|к)"
    r"|де.?анонимиз|deanonymi",
    re.IGNORECASE,
)


def _iter_content_texts(content) -> list[str]:
    """Извлечь все текстовые части content сообщения (str | list[dict] | None)"""
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [
            part["text"]
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        ]
    return []


def _resolve_local_path(path_str: str) -> Path:
    """
    Разрешить путь из <file_content path="..."> в абсолютный путь на диске.
    Поддерживаются file:// URI, абсолютные пути и относительные (от корня
    workspace).
    """
    if path_str.lower().startswith("file://"):
        path_str = unquote(urlparse(path_str).path)
        # file:///C:/... -> C:/... (Windows)
        if re.match(r"^/[A-Za-z]:/", path_str):
            path_str = path_str[1:]
    path = Path(path_str)
    if not path.is_absolute():
        workspace_candidate = (WORKSPACE_ROOT / path).resolve()
        if workspace_candidate.exists():
            return workspace_candidate
        # Ссылки агентов вроде Hermes (@file:AppData/Local/hermes/attachments/…)
        # задаются относительно домашней папки пользователя, а не workspace
        home_candidate = (Path.home() / path).resolve()
        if home_candidate.exists():
            return home_candidate
        return workspace_candidate
    return path


def _is_anonymized_copy_path(path_str: str) -> bool:
    """True, если путь указывает на анонимизированную копию (<name>.anonymized.<ext>)."""
    try:
        return _resolve_local_path(path_str).stem.lower().endswith(".anonymized")
    except Exception:
        return False


def _result_path_for(copy_path: str) -> str:
    """
    Путь к «файлу результата» для анонимизированной копии.
    <name>.anonymized.<ext> -> <name>.result.<ext> (файл результата рядом
    с копией; копия остаётся неизменным исходником для скриптов модели).
    """
    path = Path(copy_path)
    new_stem = path.stem.replace(".anonymized", ".result")
    return str(path.with_name(new_stem + path.suffix))


def last_user_message_texts(messages) -> list[str]:
    """
    Текстовые части ПОСЛЕДНЕГО user-сообщения, содержащего текст.

    Команда перехвата («анонимизируй…», «деанонимизируй…») должна искаться
    только в текущем ходе пользователя: старые команды остаются в истории
    диалога навсегда и иначе ложно перехватывают последующие запросы
    («сравни два файла», «составь отчёт» и т.п.).

    Служебные user-сообщения с результатами инструментов (tool_result)
    текстовых частей не содержат и пропускаются — берётся последнее
    содержательное сообщение пользователя.
    """
    for msg in reversed(list(messages)):
        if getattr(msg, "role", None) != "user":
            continue
        texts = _iter_content_texts(getattr(msg, "content", None))
        if texts:
            return texts
    return []


def norm_fs_path(path_str: str) -> str:
    """
    Канонический вид пути файловой системы для строкового сравнения
    (унификация слэшей и регистра диска в Windows). Пути вида
    «C:/a/b.docx», «c:\\a\\b.docx» и «file:///C:/a/b.docx» дают одинаковый
    результат.
    """
    try:
        resolved = _resolve_local_path(path_str)
    except Exception:
        resolved = Path(path_str)
    return os.path.normcase(os.path.normpath(str(resolved)))
