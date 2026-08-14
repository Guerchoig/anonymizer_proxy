# Anonymizer Proxy — Ручное ревью (VS Code extension)

Тонкое расширение VS Code, реализующее UI ручного ревью анонимизированных
промптов для [Anonymizer Proxy](../README.md).

## Что делает

1. Опрашивает прокси (`GET /api/review/pending`) и показывает список запросов,
   ожидающих ручного ревью, в сайдбаре и в статус-баре.
2. Для каждого запроса доступны действия:
   - **Открыть .md** — открыть канонический анонимизированный файл в редакторе;
   - **Одобрить (с правками)** — прочитать текущее содержимое `.md` и отправить
     его как `edited_content` (`POST /api/review/approve`);
   - **Одобрить (без правок)** — отправить одобрение без изменений;
   - **Отклонить** — отклонить запрос (с опциональной причиной).

## Сборка и запуск

```powershell
cd vscode-extension
npm install
npm run compile
```

Затем в VS Code: **Run → Start Debugging** (`F5`) — запустится окно Extension
Development Host с расширением.

## Настройки

| Настройка | По умолчанию | Описание |
|-----------|--------------|----------|
| `anonymizerProxy.baseUrl` | `http://127.0.0.1:8081` | Базовый URL прокси |
| `anonymizerProxy.apiToken` | (пусто) | `PROXY_API_TOKEN` для `/api/*` (если задан в `.env` прокси) |
| `anonymizerProxy.pollIntervalMs` | `2000` | Интервал опроса очереди ревью |

Если прокси слушает `localhost` без `PROXY_API_TOKEN`, токен можно оставить
пустым — `/api/*` эндпоинты будут доступны без авторизации.

## Требования

- Запущенный Anonymizer Proxy (`python -m anonymizer_proxy.main` или
  `uvicorn anonymizer_proxy.main:app`).
- Прокси в режиме `review` (запрос с `mode: "review"` или
  `ANONYMIZER_MODE=review` в `.env` прокси).
