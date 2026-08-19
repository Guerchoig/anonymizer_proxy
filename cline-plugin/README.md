# Anonymizer Proxy — Cline SDK-плагин

Плагин добавляет в Cline инструменты для ручного управления анонимизацией
через [Anonymizer Proxy](../README.md):

| Инструмент | Эндпоинт | Что делает |
|---|---|---|
| `anonymize_text(text)` | `POST /api/anonymize` | Анонимизирует текст, возвращает анонимизированный текст + `session_id` + путь `.md` |
| `anonymize_file(file_path)` | `POST /api/anonymize_file` | Создаёт копию `<name>.anonymized.<ext>` с плейсхолдерами + `.md` для ревью |
| `send_prompt(session_id, content)` | `POST /api/send` | Отправляет анонимизированный промпт в облако, возвращает де-анонимизированный ответ |
| `deanonymize_file(file_path, session_id)` | `POST /api/deanonymize_file` | Заменяет плейсхолдеры в файле на реальные значения (финальный шаг) |

## Настройка (через переменные окружения)

- `ANONYMIZER_PROXY_URL` — базовый URL прокси (по умолчанию `http://127.0.0.1:8081`).
- `ANONYMIZER_PROXY_TOKEN` — `PROXY_API_TOKEN`, если задан в `.env` прокси.

## Установка

Cline (расширение и CLI) обнаруживает плагины, сканируя каталоги:

1. `<корень workspace>/.cline/plugins/<папка-плагина>/`
2. `~/.cline/plugins/<папка-плагина>/` (глобально, для всех workspace)
3. `~/Documents/Cline/Plugins/<папка-плагина>/`

В каждой папке плагина ищется `package.json` с секцией `cline.plugins` (поле
`paths`) либо `index.ts` / `index.js`.

> **Важно:** Cline ищет плагины от **корня открытого workspace** (не обязательно
> от папки проекта с прокси) и из глобального `~/.cline/plugins`. Каталог
> `cline-plugin/` внутри репозитория **не** является путём поиска — сам по себе
> он не подхватывается.

### Вариант 1: глобально (рекомендуется)

Скопируйте папку плагина в глобальный каталог (без `node_modules` — `@cline/core`
разрешается из хоста Cline):

```powershell
robocopy .\cline-plugin "$env:USERPROFILE\.cline\plugins\anonymizer-proxy-plugin" /E /XD node_modules
```

### Вариант 2: только для текущего workspace

```powershell
robocopy .\cline-plugin <корень-workspace>\.cline\plugins\anonymizer-proxy-plugin /E /XD node_modules
```

### Вариант 3: CLI (если установлен `cline` и доступен npm)

```bash
cline plugin install ./cline-plugin
```

Команда копирует плагин в `<корень workspace>/.cline/plugins/_installed/local/<имя>-<hash>/`
(при запуске из корня workspace) — обратите внимание, что это тоже привязка к workspace.

После установки **перезапустите Cline** (перезагрузите окно VS Code) — плагины
загружаются при старте сессии.

## Сценарий работы

1. «Анонимизировать» → попросить модель вызвать `anonymize_text` / `anonymize_file`.
2. Просмотреть/отредактировать `.md` (или анонимизированную копию файла).
3. «Отправить» → `send_prompt` (де-анонимизированный ответ).
4. Если модель меняла файл → в конце `deanonymize_file`.
