# Публикация изменений и выпуск релизов

> Этот документ описывает полный цикл: от коммита до опубликованного релиза
> на GitHub. Все команды проверены на практике; автоматизация — два workflow:
> `tests.yml` (тесты, включая macOS) и `release.yml` (сборка релиза).

## Общая схема

```
изменения → git add/commit → git push master
                                  ↓
        GitHub Actions «Tests» (tests.yml: ubuntu + macos)
        - тесты пакета (ubuntu + macos) + кроссплатформенный аудит;
        - E2E macOS-инсталлятора: install.sh → selftest → .app → /health
                                  ↓ (Tests зелёный)
        git tag -a vX.Y.Z -m "…" && git push origin vX.Y.Z
                                  ↓
              GitHub Actions (workflow "Release", ubuntu-latest)
                                  ↓
        git archive → 2 zip (windows / macos) → release_check.py
                                  ↓
              GitHub Release + ассеты + авто-changelog
```

- **Тесты до релиза**: workflow «Tests» запускается на каждый пуш в master
  и на PR. Тег ставится ТОЛЬКО если последний запуск Tests на master зелёный:
  `gh run list --workflow=tests.yml --repo Guerchoig/anonymizer_proxy --limit 1`.
- **Что собирается**: `git archive` берёт ТОЛЬКО отслеживаемые файлы —
  `.env`, `data/`, `.venv`, `*.docx` и прочее из `.gitignore` в архив
  не попадут; `.gitattributes` (`export-ignore`) дополнительно вырезает
  `.github/` и сам `.gitattributes`.
- **Контроль чистоты**: `scripts/release_check.py` проверяет архив на
  отсутствие секретов/PII (`.env`, `data/`, `.venv/`, `*.docx|xlsx|pptx`,
  `*.db`) — при нарушении сборка падает, релиз не публикуется.
- **Архивы платформо-независимы** (исходники + скрипты установки); два
  файла — `-windows.zip` и `-macos.zip` — для очевидности пользователю.

## Семантика версий (SemVer)

Версия задаётся тегом `v<MAJOR>.<MINOR>.<PATCH>` и **дублируется** в
`anonymizer_proxy/config.py` → `PROXY_VERSION` (виден в `/health`,
`/api/status` и баннере при старте — по нему можно проверить, что
запущенный процесс исполняет актуальный код).

| Изменение | Пример | Версия |
|---|---|---|
| Новая функциональность, совместимая со старыми настройками | LLM-роутер, office_ops | **MINOR +1** (`v1.2.2` → `v1.3.0`) |
| Исправления, документация, мелкие доработки | кракозябры в .env, README | **PATCH +1** (`v1.2.1` → `v1.2.2`) |
| Несовместимые изменения (смена `.env`-ключей, формата БД) | — | **MAJOR +1** (пока не использовался) |

Исторические паттерны: docs-only релизы выпускались и как PATCH; при
серии экспериментальных сборок лишние релизы удаляются (см. ниже).

## Шаг 1. Коммит и пуш изменений

Перед коммитом — проверить рабочее дерево и отфильтровать артефакты:

```powershell
cd <папка проекта>
git status --short          # ?? — неотслеживаемые: коммитить только осмысленное
git add <файлы...>          # точечно; НЕ использовать git add -A без проверки
git commit -m "feat: …"     # см. стиль сообщений ниже
git push origin master
```

- Мусор и файлы с возможным PII **не коммитятся** (dump'ы, `temp_*.txt`,
  Office-документы; `.gitignore` уже покрывает `*.docx|xlsx|pptx`, `~$*`,
  `temp_output.txt`).
- Стиль сообщений: `feat(<area>):`, `fix(<area>):`, `docs:`, `ci:`,
  `chore:` — по одному логическому изменению на коммит.
- Перед релизом функциональных изменений прогнать целевые тесты
  (`anonymizer_proxy/tests/*.py` — скрипты с кодом возврата).
  На Windows — локально (`.venv\Scripts\python.exe <тест>`); ubuntu и macOS
  проверяет CI: `gh run list --workflow=tests.yml --repo Guerchoig/anonymizer_proxy --limit 1`.

### Тесты macOS в CI (workflow «Tests», `tests.yml`)

Триггеры: пуш в master, PR, ручной запуск (workflow_dispatch). Два job'а:

| Job | Раннер | Что делает |
|---|---|---|
| `unit-tests` | ubuntu-latest + macos-latest | `uv sync --locked --extra cpu` (как в install.sh), `scripts/check_crossplatform.py`, все `anonymizer_proxy/tests/test_*.py` (код возврата) |
| `macos-installer` | macos-latest | E2E-тест macOS-инсталлятора: `bash -n`/`zsh -n` всех скриптов → `bash install.sh < /dev/null` (реальная установка: uv, .env, скачивание моделей) → `install_selftest.py` с обязательным кодом 0 → проверка `.env` (`NER_DEVICE=mps`, токен) → `make_mac_app.sh` + `plutil -lint` → `start_proxy.sh` и опрос `/health` → `install_launchagent.sh` (не блокирующе) |

Windows в CI не гоняется: это машина разработки, тесты прогоняются локально
перед тегом. macOS в CI — главный барьер против регрессий вида
`subprocess.CREATE_*`, `.venv\Scripts` и прочих Windows-only конструкций
(статический аудит — `scripts/check_crossplatform.py`, динамический —
реальный запуск инсталлятора и сервера на macos-latest).

## Шаг 2. Выпуск релиза

```powershell
git tag -a v1.4.1 -m "Anonymizer Proxy 1.4.1 — краткое описание"
git push origin v1.4.1
```

Или через GitHub CLI (создаёт и тег, и релиз в веб-интерфейсе):

```powershell
gh release create v1.4.1 --repo Guerchoig/anonymizer_proxy --title "v1.4.1" --notes "…"
```

После пуша тега запускается workflow «Release» (~1–2 мин). Контроль:

```powershell
gh run list --repo Guerchoig/anonymizer_proxy --limit 3     # статус сборки
gh run list --workflow=tests.yml --repo Guerchoig/anonymizer_proxy --limit 1  # тесты (macOS в т.ч.)
gh release view v1.4.1 --repo Guerchoig/anonymizer_proxy    # релиз и ассеты
```

Ожидаемый результат: статус `✓ completed/success`, ассеты
`anonymizer-proxy-vX.Y.Z-macos.zip` и `anonymizer-proxy-vX.Y.Z-windows.zip`
в состоянии `uploaded`, аннотаций 0. Если сборка упала — см. «Диагностика».

## Шаг 3. Актуализация PROXY_VERSION

Не забудьте поднять `PROXY_VERSION` в `anonymizer_proxy/config.py` в том же
коммите, что и изменения (или перед тегом): версия в `/health` должна
совпадать с тегом, иначе непонятно, какой код реально исполняется.

## Удаление ошибочного релиза

Случайно выпущенный релиз удаляется вместе с тегом (проверено на v1.4.1):

```powershell
gh release delete vX.Y.Z --repo Guerchoig/anonymizer_proxy --yes --cleanup-tag
git tag -d vX.Y.Z                      # локальный тег
git push origin :refs/tags/vX.Y.Z      # если тег успел уйти отдельно
```

Коммиты при этом не трогаются — контент остаётся в master.

## Требования к окружению публикующего

- Права: **admin/maintain** на репозиторий (создание релиза + пуш тега).
- GitHub CLI (`gh`) с токеном, имеющим scopes `repo` + `workflow`
  (используется и для диагностики приватного репозитория — анонимный API
  для него отвечает 404).
- Git: пуш по HTTPS с сохранёнными учётными данными (Windows Credential
  Manager).

## Диагностика сбоев сборки

| Симптом | Причина и решение |
|---|---|
| Сборка падает на `release_check.py` («В архив попали запрещённые файлы») | В git просочился файл из запрещённого списка — убрать из индекса (`git rm --cached …`), добавить в `.gitignore`, перевыпустить тег |
| Ассет не появился, сборка `success` | Смотреть лог run'а: `gh run view <id> --repo … --log-failed` |
| `gh` отвечает 404 на приватном репо | Аутентификация: `gh auth status`; повторный `gh auth login` |
| README в архиве устарел | Документационные коммиты попали после тега — перевыпустить: удалить тег/релиз, создать заново на актуальном коммите |
| Workflow не запустился | Проверить, что пуш именно тега `v*` (пуш коммитов тег не триггерит); Actions включён в Settings |
| Упал job `macos-installer` в workflow «Tests» | Инсталлятор сломан на macOS — релиз ставить нельзя. Смотреть лог: `gh run view <id> --repo Guerchoig/anonymizer_proxy --log-failed`; частая причина — Windows-only код, который не поймал статический аудит |
| Предупреждение «Node.js 20 is deprecated» | Устарело: actions обновлены до Node 24 (`checkout@v5`, `gh-release@v3`); не пиновать старые версии |

## Диагностика сбоев коммита

| Симптом | Причина и решение |
|---|---|
| `fatal: pathspec '…' did not match any files` — после `git add` ничего не staged, коммит не состоялся | Путь указан **по памяти**, а файл лежит в другом месте (реальный кейс: `env_editor.py` был в `anonymizer_proxy/`, а не в `anonymizer_proxy/proxy/`). Сначала `git status --short` — пути в нём фактические; добавлять файлы по списку из него, не по предположениям. Если параллельно работал другой чат — структура могла измениться под вами, перечитать статус перед каждым `git add` |
| В `git status` появились незнакомые `??`-файлы | Другая сессия/чат работает в том же репо одновременно. Не добавлять их вслепую (`git add -A` запрещён): каждый файл проверить — код/док или артефакт с возможным PII (`temp_*.txt`, дампы). Рабочее дерево может измениться между вашими шагами — перечитывать статус |
| Коммит «пустой» / не создался | `git add` упал на части списка — staged пуст; исправить пути и повторить `git add` + `git commit` |

## История значимых изменений процесса

- v1.0.0–v1.0.1: workflow только `-windows.zip` (на windows-latest).
- v1.0.2: попытка matrix (windows+macos) — упала (`python3` нет в bash
  windows-раннера); из урока: сборка перенесена на ubuntu-latest.
- v1.0.5+: два архива из одной задачи на Ubuntu; `actions/checkout@v5`,
  `gh-release@v3` (Node 24, предупреждения ушли).
- CI-тесты: добавлен workflow `tests.yml` (ubuntu + macos) — unit-тесты
  пакета и E2E-тест macOS-инсталлятора (install.sh → selftest → .app →
  /health); правило «тег только при зелёном Tests на master».
- Приватность: репозиторий приватный; все проверки — через `gh api`.
