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

```bash
cline plugin install ./cline-plugin
```

## Сценарий работы

1. «Анонимизировать» → попросить модель вызвать `anonymize_text` / `anonymize_file`.
2. Просмотреть/отредактировать `.md` (или анонимизированную копию файла).
3. «Отправить» → `send_prompt` (де-анонимизированный ответ).
4. Если модель меняла файл → в конце `deanonymize_file`.
