# ПЛАН: переход на общий llama-рантайм (llama-server) — anonymizer_proxy

**Статус:** ВЫПОЛНЕНО — код (§2) + доводка и приёмка (§3.A/§3.B, 2026-09-25) +
macOS (§3.C, 2026-09-25).
Осталось опциональное: §3.D (развитие).
**Дата сверки с кодом:** 2026-09-25.
**Родственный план:** `PLAN_SHARED_LLAMA_RUNTIME.md` в hermes-disk-search —
планы выполняются синхронно (см. §6).

## 0. Цель

Один llama-server и один набор GGUF-моделей на машину для обоих проектов:

- одна сборка llama.cpp (cuda|vulkan) с комплектом DLL — без дублирования и
  расхождений по вариантам/версиям между проектами;
- одни и те же GGUF-модели (в т.ч. общая чат-модель) лежат в одном каталоге;
- смена активной чат-модели — одной командой/кнопкой, видна обоим проектам
  сразу (их llama-инстансы перезапускаются автоматически);
- установка/переустановка любого проекта идемпотентна: второй проект ничего
  не докачивает, если компоненты уже на месте.

## 1. Архитектура («Вариант C» — общий рантайм уровня машины)

Каталог: `%LOCALAPPDATA%\llama-runtime` (переопределяется `LLAMA_RUNTIME_DIR`).

```
llama-runtime\
  bin\                      llama-server.exe + все DLL (одна сборка cuda|vulkan)
  models\chat\              GGUF чат-моделей + current.json (активная)
  models\embedding\         bge-m3
  models\rerank\            bge-reranker-v2-m3
  projects.json             реестр проектов для перезапуска их инстансов
  version.json              вариант сборки (cuda|vulkan) + тег llama.cpp
```

Ключевые механизмы:

- **спецификатор `shared:<role>`** в конфигах — «файл из манифеста общего
  рантайма» (`models\<role>\current.json`): смена активной модели не требует
  правки конфигов проектов;
- **`anonymizer_proxy/llama_runtime.py`** — общий модуль (SYNC-COPY с
  `hds/llama_runtime.py` в hermes): пути, поиск бинаря, резолв модели,
  манифест, реестр проектов, пресеты моделей, `switch_chat_model`
  (манифест + перезапуск инстансов всех проектов), CLI;
- **`scripts/ensure_llama_runtime.ps1`** — идемпотентный установщик рантайма
  (SYNC-COPY с `hermes-disk-search/installers/ensure_llama_runtime.ps1`):
  бинарь нужного варианта (+CUDA cudart), GGUF-пресеты, манифест, регистрация
  проекта в `projects.json`;
- **порты не меняются**: proxy использует 8080 (свой llama-инстанс), hermes —
  8010–8012; общие файлы на диске, инстансы и флаги у каждого свои.

## 2. Выполнено (сверено с кодом)

- [x] `anonymizer_proxy/llama_runtime.py` — модуль + CLI
      (`python -m anonymizer_proxy.llama_runtime dir|list|switch|download|register`);
- [x] `scripts/ensure_llama_runtime.ps1` — установка бинаря и моделей в
      рантайм, регистрация проекта;
- [x] миграция машины: бинарь (CUDA-сборка) и модели перенесены из
      hermes-disk-search в рантайм; `version.json`, `current.json`
      (активная: `Qwen3.5-9B-Q6_K.gguf`), `projects.json` (оба проекта);
- [x] `anonymizer_proxy/llm_server.py`: цепочка поиска бинаря
      `LLM_SERVER_BIN → общий рантайм → BASE_DIR\tools\llama.cpp → PATH`
      (заодно исправлен поиск от `cwd` — теперь от корня проекта);
      `build_command` резолвит `shared:chat`;
- [x] `.env` / `.env.example`: `LLM_SERVER_MODEL=shared:chat`;
- [x] `main.py`: `GET/POST /api/chat-model` — список файлов/пресетов, фоновая
      смена модели с прогрессом, перезапуск своего llama-инстанса и инстансов
      проектов из `projects.json`;
- [x] `static/env_editor.html`: виджет «Общая чат-модель (llama.cpp)»
      (селект файлов+пресетов, «Применить во всех проектах», поллинг прогресса);
- [x] `scripts/install_selftest.py` — шаг 5/5 понимает `shared:chat` и рантайм;
- [x] `scripts/download_llm_model.py` — качает в `models\chat` рантайма и
      обновляет манифест;
- [x] `install.ps1` шаг 3 → вызов `ensure_llama_runtime.ps1 -Models chat`
      (убрано дублирование логики скачивания);
- [x] тесты: `tests/test_llama_runtime.py` (5/5), `test_llm_server`,
      `test_env_editor`, `test_launcher` — проходят;
- [x] живая проверка: llama-server proxy стартует из общего рантайма
      (порт 8080, `state=llama`, модель из `models\chat`).

## 3. Ход выполнения

### A. Доводка (Windows) — ВЫПОЛНЕНО 2026-09-25

1. **Перезапуск прокси** — не требуется: на момент доводки процесс прокси
   (8081) не был запущен, новый код и эндпоинты подхватятся при ближайшем
   старте (`start_proxy.cmd` / `python -m anonymizer_proxy.main`). На 8080
   уже жил llama-server общего рантайма (переиспользуется, не перезапускался).
2. **README.md** — сделано:
   - в таблице переменных `LLM_SERVER_MODEL` = `shared:chat` (общая модель
     рантайма; абсолютный путь — escape-hatch), `LLM_SERVER_BIN` —
     автопоиск включает общий рантайм;
   - добавлен раздел «Общий llama-рантайм и смена чат-модели» (раскладка
     каталога, `shared:<role>`, кнопка в `/env-editor`, CLI
     `llama_runtime switch <файл> [--no-download] [--no-restart]`,
     `list`/`dir`, установщик `ensure_llama_runtime.ps1`, escape-hatch/откат);
   - в таблицу «Ключевые компоненты» добавлены `llm_server.py` и
     `llama_runtime.py`;
   - описание группы «Локальная модель (llama.cpp)» в `env_editor.py`
     приведено к `shared:chat`.
3. **Удалён пустой `data\models\llm`** — модели живут только в рантайме.
4. **`scripts\install_selftest.py`** — PASS, шаг 5/5 зелёный: бинарь и GGUF
   найдены в `%LOCALAPPDATA%\llama-runtime`, живой инстанс на 8080
   (`state=llama`, `total_slots=1`).
5. **Дополнительно (флейки-тест)**: `tests/test_llm_server.py`
   `test_cli_check_exit_codes` переведён на эфемерный порт мини-сервера —
   тест больше не требует, чтобы боевой `LLM_SERVER_PORT` был свободен
   (на машине с общим рантаймом там штатно живёт llama-server).

### B. Приёмка (команды)

Результат прогона 2026-09-25 (все зелёные):

```powershell
Set-Location c:\Test\anonymizer_proxy
.venv\Scripts\python.exe -m anonymizer_proxy.llm_server check      # exit 0, state=llama total_slots=1
.venv\Scripts\python.exe -m anonymizer_proxy.llama_runtime list     # current=Qwen3.5-9B-Q6_K.gguf, binary_ok=true, 2 проекта
.venv\Scripts\python.exe -m anonymizer_proxy.tests.test_llama_runtime   # 5/5 PASS
.venv\Scripts\python.exe -m anonymizer_proxy.tests.test_llm_server      # 13/13 PASS
.venv\Scripts\python.exe -m anonymizer_proxy.tests.test_env_editor      # PASS
.venv\Scripts\python.exe -m anonymizer_proxy.tests.test_launcher        # PASS
.venv\Scripts\python.exe scripts\install_selftest.py                    # PASS
```

Ручной сценарий (осталось для оператора): в `/env-editor` выбрать модель →
«Применить везде» → в строке статуса появится отчёт по проектам
(`anonymizer_proxy: ok`, `hermes-disk-search: ok`).

### C. macOS (фаза 2) — ВЫПОЛНЕНО (2026-09-25)

- [x] `llama_runtime.py` в обеих копиях вычисляет на darwin каталог
      `~/Library/Application Support/llama-runtime` (остальные ОС — как были:
      `%LOCALAPPDATA%` / XDG); общие подсказки `install_hint()`;
- [x] `scripts/ensure_llama_runtime.sh` — SYNC-COPY-копия из
      `hermes-disk-search/installers/` (сверено побайтово, sha256 совпадает);
      macOS-ветки: рантайм-каталог, вариант `metal`, llama.cpp из Homebrew
      ссылкой в `bin/` либо пре-билд `-bin-macos-arm64` с GitHub Releases
      (карантин снимается `xattr -dr`);
- [x] `install.sh` приведён к паритету с `install.ps1`: шаг 0 — снятие
      карантина Gatekeeper (`xattr -dr com.apple.quarantine .`) и `chmod +x`
      на `*.sh`/`*.command`; шаг 1 — preflight (Apple Silicon, macOS 12+,
      ОЗУ ≥ 8 ГБ, диск ≥ 10 ГБ; пороги переопределяются
      `ANONYMIZER_MIN_MACOS|_RAM_GB|_DISK_GB`, Intel — `ANONYMIZER_ALLOW_INTEL=1`);
      шаг 4 — вызов `scripts/ensure_llama_runtime.sh --models chat
      --project-name anonymizer_proxy --restart-args "-m anonymizer_proxy.llm_server
      restart"` (мягкий отказ: без рантайма работают облачные бэкенды);
      шаг 7 — скачивание GGUF убрано (его делает рантайм), вместо него
      подсказка про `download_llm_model.py`; финал — каталог рантайма и
      команды `llama_runtime list|switch`, `llm_server status`;
- [x] `install.command` — установка двойным кликом из Finder (вызывает
      `install.sh`, окно держится до Enter);
- [x] `.gitattributes`: `*.command text eol=lf` (иначе в релизный архив могла
      попасть CRLF-версия — .command не запускался бы двойным кликом);
      рабочие копии `*.sh`/`*.command` нормализованы в LF;
- [x] `scripts/install_selftest.py`: подсказки платформо-зависимы
      (`llama_runtime.install_hint()`, `.venv/bin/python` на macOS);
- [x] `scripts/check_crossplatform.py`: аудит дополнен macOS-инвариантами
      (наличие скриптов установки/запуска, LF у `*.sh`/`*.command`, `eol=lf`
      в `.gitattributes`, побайтовая сверка SYNC-COPY-пар — если
      hermes-disk-search рядом или задан `HDS_ROOT`);
- [x] CI (`.github/workflows/tests.yml`): в job `macos-installer` добавлены
      `zsh -n install.command`, проверка регистрации проекта в
      `projects.json` общего рантайма и проверка отказов preflight;
      штатный прогон идёт с пониженными порогами
      (`ANONYMIZER_MIN_RAM_GB=6`, `ANONYMIZER_MIN_DISK_GB=6`) — у раннера
      GitHub 7 ГБ ОЗУ;
- [x] доки: README (установка `install.command`, общий рантайм и его каталоги
      на всех ОС, LaunchAgent llama, troubleshooting карантина/preflight/без
      brew, обновление поверх), `.env.example` (каталоги рантайма, автопоиск
      бинаря), этот план, RELEASING не менялся (описывает CI как есть).

Проверки, прогнанные на машине разработки (Windows): `bash -n` для всех
`*.sh`/`*.command` (git-bash, 7/7 OK), `scripts/check_crossplatform.py`
(exit 0, SYNC-COPY — 3 пары совпадают), smoke-прогон
`scripts/ensure_llama_runtime.sh` с флагами из `install.sh` в изолированном
каталоге (graceful-отказ без brew/python3 — без падения), `install.sh` на
не-macOS хосте корректно останавливается на preflight (rc=1).

### D. Развитие (опционально)

- пресеты дополнительных чат-моделей — одна запись в `CHAT_PRESETS`
  (`llama_runtime.py`, обе копии) + таблица `$presets` в
  `ensure_llama_runtime.ps1` (обе копии) + `DEFAULT_CHAT`;
- третий проект-потребитель: копия `llama_runtime.py` + вызов
  `ensure_llama_runtime.ps1 -ProjectName … -RestartArgs …`;
- вынос таблицы пресетов в JSON рантайма (сейчас дублируется в SYNC-COPY
  парах).

## 4. Откат / escape-hatch

- `LLM_SERVER_BIN` — явный путь к бинарю мимо рантайма;
- `LLM_SERVER_MODEL` — абсолютный путь к GGUF мимо манифеста (приоритетнее
  `shared:chat`) — эксперименты с моделью, не затрагивая hermes;
- `LLAMA_RUNTIME_DIR` — перенести рантайм в другой каталог;
- полный откат миграции: вернуть файлы из рантайма в `tools\llama.cpp` и
  `data\models\llm`, вернуть прежние значения `.env`.

## 5. Риски

- смена модели прерывает текущие локальные запросы (диалог подтверждения в
  UI; переключение — явное действие);
- ~8.7 ГБ моделей размещены на диске C: — при переносе обновить
  `LLAMA_RUNTIME_DIR`;
- имена файлов в `current.json` регистрозависимы: резолвер подбирает файл
  без учёта регистра (учтено в коде);
- инстанс, запущенный вручную мимо менеджера, не попадает в перезапуск при
  смене модели.

## 6. Синхронизация с планом hermes-disk-search

- **SYNC-COPY-пары** (правятся всегда вместе, содержимое идентично):
  `anonymizer_proxy/llama_runtime.py` ↔ `hds/llama_runtime.py`;
  `scripts/ensure_llama_runtime.ps1` ↔ `installers/ensure_llama_runtime.ps1`.
  Проверка: совпадение `Get-FileHash` обеих пар.
- порядок действий при изменениях — см. зеркальную секцию §6 в плане hermes.

