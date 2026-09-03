# Отчёт: Структура папок Hermes Agent Desktop (Windows)

> Источники:
> - https://hermes-agent.nousresearch.com/docs/user-guide/configuration
> - https://hermes-agent.nousresearch.com/docs/user-guide/windows-native
> - https://hermes-agent.nousresearch.com/docs/user-guide/desktop
> - https://blog.dailydoseofds.com/p/the-anatomy-of-hermes-folder

---

## Где лежит корневая папка

В Windows корневая папка Hermes (`HERMES_HOME`) — это:

```
C:\Users\<имя_пользователя>\AppData\Local\hermes\
```

Она же доступна через `%LOCALAPPDATA%\hermes\`. CLI добавляет `%LOCALAPPDATA%\hermes\bin` в User PATH, там лежит `hermes.exe`.

> ⚠️ **Известный баг Desktop GUI** (issue на GitHub, метки `bug / comp/desktop / platform/windows`):
> Electron-оболочка иногда ищет конфиг в `C:\Users\<user>\.hermes\`, тогда как CLI кладёт его
> в `AppData\Local\hermes\`. Если Desktop GUI игнорирует правки `config.yaml` — проверьте,
> не в двух ли местах у вас папки Hermes, и синхронизируйте их (или задайте `HERMES_HOME` явно).

---

## Полная структура `~/.hermes/`

```
~/.hermes/                                  (= %LOCALAPPDATA%\hermes\)
│
├── config.yaml          # Главный конфиг: модель, terminal backend,
│                        # TTS, компрессия, MCP-серверы, включение инструментов
├── .env                 # API-ключи и секреты (ANTHROPIC_API_KEY и т.п.)
├── auth.json            # OAuth-токены провайдеров (Nous Portal и др.)
│
├── SOUL.md              ★ ГЛАВНЫЙ СИСТЕМНЫЙ ПРОМПТ — идентичность агента
│                        # Занимает слот #1 в system prompt, полностью
│                        # заменяет встроенную идентичность.
│                        # Лимит: context_file_max_chars (по умолчанию 20 000)
│
├── memories/            # Долговременная память (вставляются в system prompt
│   ├── MEMORY.md        #   как frozen snapshot в начале сессии)
│   │                    #   ~2 200 символов: конвенции, уроки, нюансы инструментов
│   └── USER.md          #   ~1 375 символов: профиль пользователя
│
├── skills/              # Навыки — самообучаемые способности агента
│   └── <skill_name>/
│       ├── SKILL.md         # Процедура/инструкция навыка
│       ├── references/      # Документация, которую читает агент
│       └── scripts/         # Исполняемые хелперы
│
├── sessions/            # Сохранённые сессии/диалоги
├── trajectories/        # Траектории выполнения (для отладки/анализа)
├── logs/                # Логи работы
│
└── bin\
    └── hermes.exe       # Сам CLI-бинарник
```

---

## Системные промпты — где лежат и как загружаются

Hermes собирает system prompt из нескольких источников с **приоритетом**
(загружается только один тип project-контекста — первый найденный):

| Файл | Назначение | Область поиска |
|---|---|---|
| **`~/.hermes/SOUL.md`** ★ | Идентичность агента, слот #1 system prompt | Глобально, всегда |
| `.hermes.md` / `HERMES.md` | Инструкции под конкретный проект (наивысший приоритет) | От текущей папки вверх до корня git |
| `AGENTS.md` | Конвенции кодинга проекта | Рекурсивный обход подкаталогов (все найденные склеиваются) |
| `CLAUDE.md` | Контекст Claude Code (подхватывается для совместимости) | Только рабочая директория |
| `.cursorrules` | Правила Cursor IDE | Только рабочая директория |
| `.cursor/rules/*.mdc` | Rule-файлы Cursor | Только рабочая директория |

**Порядок резолва project-контекста** (first match wins):
`.hermes.md` → `AGENTS.md` → `CLAUDE.md` → `.cursorrules`.
`SOUL.md` загружается **всегда отдельно**, независимо от этого порядка.

Если `SOUL.md` отсутствует, пустой или не читается — Hermes использует
встроенную идентичность по умолчанию. При первом запуске создаётся
дефолтный `SOUL.md` автоматически.

Все загруженные контекстные файлы обрезаются до `context_file_max_chars`
(по умолчанию 20 000 символов) со «умной» усечкой.

---

## Как редактировать

- **Быстрый способ:** `hermes setup --portal` — OAuth-настройка без ручного редактирования YAML.
- **Модель:** правится `config.yaml` (поле `model`) или команда `hermes model`.
- **Идентичность агента:** редактирование `~/.hermes/SOUL.md` — это и есть «системный промпт» в привычном понимании.
- **Проектные правила:** создать `.hermes.md` или `AGENTS.md` в корне репозитория.

---

## Полезные переменные окружения

| Переменная | Назначение |
|---|---|
| `HERMES_HOME` | Переопределить путь к корневой папке (по умолчанию `%LOCALAPPDATA%\hermes`) |
| `HERMES_DESKTOP_CWD` | Стартовая папка проекта для Desktop-чатов |
| `HERMES_DESKTOP_HERMES_ROOT` | Переопределить корень репозитория для Desktop-приложения |

---

## Особенности Windows-установки

- После установки нужно **открыть новое окно PowerShell** — существующие терминалы не подхватят обновлённый PATH.
- Автозапуск шлюза при логине: задача `HermesGateway` через `schtasks /SC ONLOGON /RL LIMITED` (без UAC); при блокировке групповой политикой — ярлык в `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup`.
- Gateway запускается через `pythonw.exe` (без консоли) — защищает от `CTRL_C_EVENT` из соседних процессов.
- Функции, недоступные в Native Windows (в отличие от WSL2): dashboard `/chat` embedded terminal pane (нужен POSIX PTY).
