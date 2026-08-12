# Anonymizer Proxy

Прокси-сервер для анонимизации конфиденциальных данных перед отправкой запросов в облачные LLM.

## Архитектура

```
┌─────────────────────────────────────────────────────────────────┐
│                         ВАШ ПК (RTX 3060)                        │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌──────────┐     ┌─────────────────────────────────────────┐  │
│  │  Cline   │────▶│  FastAPI Прокси (localhost:8080)          │  │
│  │          │◀────│  ├─ Парсер файлов (DOCX/XLSX/XML)       │  │
│  └──────────┘     │  ├─ NER-сервис ──┐                      │  │
│                   │  ├─ Маппинг (SQLite)│                    │  │
│                   │  └─ Де-анонимизатор │                    │  │
│                   └─────────────────────┼────────────────────┘  │
│                                         │                        │
│  ┌──────────────────────────────────────▼────────────────────┐  │
│  │  LM Studio (localhost:1234) — Qwen3.7-9B-Q4              │  │
│  │  OpenAI-compatible API                                    │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
                                     │ HTTPS
                                     ▼
                    ┌─────────────────────────────────┐
                    │  OpenRouter → Qwen3.7-Max       │
                    └─────────────────────────────────┘
```

## Возможности

- **Анонимизация текста**: Имена, должности, подразделения, компании, адреса, продукты
- **Анонимизация файлов**: DOCX (Word), XLSX (Excel), XML (MS Project)
- **Двухуровневый NER**: Regex-паттерны + LM Studio (Qwen3.7-9B-Q4)
- **Логирование**: Все запросы/ответы сохраняются в SQLite
- **Хранение файлов**: Оригиналы и анонимизированные версии для оценки качества
- **Режим "только анонимизация"**: Без отправки в облако

## Установка

### 1. Установка зависимостей

```bash
cd c:\Test\anonymizer_proxy
pip install -r requirements.txt
```

### 2. Настройка переменных окружения

Создайте файл `.env` в директории `anonymizer_proxy`:

```env
# OpenRouter API ключ
OPENROUTER_API_KEY=sk-or-v1-ваш-ключ-тут

# Модель в OpenRouter
OPENROUTER_MODEL=qwen/qwen-3.7-max

# LM Studio (локальная NER-модель)
LM_STUDIO_URL=http://localhost:1234/v1

# Настройки прокси
PROXY_HOST=0.0.0.0
PROXY_PORT=8080

# Режим работы: full или anonymize_only
ANONYMIZER_MODE=full
```

### 3. Подготовка LM Studio

1. Запустите **LM Studio**
2. Загрузите модель **Qwen3.7-9B-Q4** (или другую подходящую)
3. Включите **Local Server** на порту **1234**
4. Включите **CORS** в настройках (Settings → Developer → Enable CORS)

## Запуск

```bash
cd c:\Test
python -m anonymizer_proxy.main
```

Или напрямую:

```bash
cd c:\Test\anonymizer_proxy
python main.py
```

Сервер запустится на `http://localhost:8080`

Документация API: `http://localhost:8080/docs`

## Настройка Cline

### Вариант 1: OpenAI Compatible

В настройках Cline:

1. **API Provider**: `OpenAI Compatible`
2. **Base URL**: `http://localhost:8080/v1`
3. **API Key**: `sk-any-string` (прокси подставит настоящий ключ)
4. **Model ID**: `qwen/qwen-3.7-max` (или ваша модель)

### Вариант 2: Использование заголовка сессии

Для сохранения маппингов между запросами можно передавать `X-Session-Id` в заголовках.

## API Endpoints

### OpenAI-совместимые

| Метод | Путь | Описание |
|-------|------|----------|
| POST | `/v1/chat/completions` | Chat completion с анонимизацией |
| GET | `/v1/models` | Список доступных моделей |

### Анонимизация

| Метод | Путь | Описание |
|-------|------|----------|
| POST | `/api/anonymize` | Только анонимизация (без облака) |
| POST | `/api/deanonymize` | Де-анонимизация по session_id |

### Логирование

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/logs` | Получить логи |
| GET | `/api/logs/export` | Экспорт логов в JSON |

### Сервисные

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/health` | Проверка работоспособности |
| GET | `/api/status` | Статус сервиса |
| POST | `/api/cleanup` | Очистка истёкших сессий |

## Примеры использования

### 1. Chat completion через прокси

```bash
curl -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen/qwen-3.7-max",
    "messages": [
      {"role": "user", "content": "Проанализируй документ: Иванов Иван Иванович, ООО Ромашка, паспорт 4500 123456"}
    ]
  }'
```

### 2. Только анонимизация (без отправки в облако)

```bash
curl -X POST http://localhost:8080/api/anonymize \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Сотрудник Петров П.П. из отдела кадров, тел. +7(999)123-45-67"
  }'
```

Ответ:
```json
{
  "session_id": "abc123...",
  "anonymized_text": "Сотрудник [PERSON_1] из [DEPARTMENT_1], тел. [PHONE_1]",
  "entities_found": [...],
  "mappings_count": 3
}
```

### 3. Анонимизация файла

```bash
# Кодируем файл в base64
FILE_B64=$(base64 -w 0 document.docx)

curl -X POST http://localhost:8080/api/anonymize \
  -H "Content-Type: application/json" \
  -d "{
    \"files\": [{\"filename\": \"document.docx\", \"content\": \"$FILE_B64\"}]
  }"
```

### 4. Де-анонимизация

```bash
curl -X POST "http://localhost:8080/api/deanonymize?session_id=abc123&text=[PERSON_1] работает в [ORG_1]"
```

## Категории PII

| Категория | Описание |
|-----------|----------|
| PERSON | Имена, фамилии, отчества |
| POSITION | Должности |
| DEPARTMENT | Подразделения |
| ORG | Компании, юрлица |
| LOC | Географические названия |
| PRODUCT | Продукты, системы |
| PASSPORT | Паспортные данные |
| PHONE | Телефоны |
| EMAIL | Email адреса |
| INN | ИНН, ОГРН, КПП |
| MONEY | Суммы денег |

## Структура данных

```
anonymizer_proxy/
├── data/
│   ├── logs/              # Экспортированные логи
│   ├── anonymized_files/  # Сохранённые файлы
│   ├── mappings/          # Маппинги по сессиям
│   └── anonymizer.db      # SQLite база
├── anonymizer/
│   ├── ner_service.py     # NER (LM Studio + regex)
│   ├── file_parser.py     # Парсеры DOCX/XLSX/XML
│   ├── mapping_store.py   # Хранилище маппингов
│   └── replacer.py        # Замена токенов
├── proxy/
│   ├── openrouter_client.py  # Клиент OpenRouter
│   └── handlers.py           # Обработчики запросов
├── models/
│   └── schemas.py         # Pydantic схемы
├── config.py              # Конфигурация
├── main.py                # FastAPI приложение
└── requirements.txt       # Зависимости
```

## Производительность

| Операция | Время |
|----------|-------|
| Парсинг DOCX (50 стр) | ~100ms |
| Парсинг XLSX (1000 строк) | ~200ms |
| NER Level 1 (regex) | ~50ms |
| NER Level 2 (Qwen3.7-9B-Q4) | ~0.8-1.2s |
| Де-анонимизация | <10ms |
| **Итого overhead** | **~1-2s на запрос** |

## Устранение проблем

### LM Studio недоступен

1. Проверьте, что LM Studio запущен
2. Убедитесь, что Local Server включён на порту 1234
3. Проверьте CORS в настройках LM Studio

### OpenRouter API ошибки

1. Проверьте API ключ в `.env`
2. Убедитесь, что модель доступна в OpenRouter
3. Проверьте баланс аккаунта

### NER не находит сущности

1. Проверьте доступность LM Studio через `/health`
2. Увеличьте `max_tokens` в config.py
3. Проверьте промпт в `ner_service.py`

## Лицензия

MIT