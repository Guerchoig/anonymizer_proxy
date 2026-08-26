#!/usr/bin/env bash
# ============================================================
#  Anonymizer Proxy — установщик macOS (M1–M4)
#  Один запуск: окружение → зависимости → модели → self-test.
#  Повторный запуск идемпотентен.
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"

step() { printf '\n==> %s\n' "$1"; }

# ---------- 1. uv ----------
step "Проверка uv (менеджер окружения и Python)"
if ! command -v uv >/dev/null 2>&1; then
    echo "uv не найден — устанавливаю standalone-версию…"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi
command -v uv >/dev/null 2>&1 || { echo "[ОШИБКА] uv не установился: https://docs.astral.sh/uv/"; exit 1; }
echo "uv: $(uv --version)"

# ---------- 2. Железо ----------
# На macOS официальный onnxruntime-wheel собран только с CPUExecutionProvider,
# поэтому ставим extra 'cpu'. Ускорение на M1–M4 даёт PyTorch-фоллбэк через
# MPS — прописываем NER_DEVICE=mps в .env ниже.
step "Аппаратная конфигурация: macOS → extra 'cpu' (+ NER_DEVICE=mps для torch-фоллбэка)"

# ---------- 3. Зависимости ----------
step "Установка зависимостей (uv sync --locked --extra cpu)"
uv sync --locked --extra cpu
PY=".venv/bin/python"

# ---------- 4. Конфигурация .env ----------
step "Конфигурация .env"
if [ -f .env ]; then
    echo ".env уже существует — не трогаю (ваши настройки сохранены)."
else
    cp .env.example .env
    # PROXY_API_TOKEN — криптостойкий случайный токен
    TOKEN=$("$PY" -c "import secrets; print(secrets.token_urlsafe(32))")
    sed -i '' "s/PROXY_API_TOKEN=REPLACE_WITH_RANDOM_TOKEN/PROXY_API_TOKEN=$TOKEN/" .env
    # На M1–M4 torch-фоллбэк ускоряется через Metal (MPS)
    sed -i '' "s/NER_DEVICE=cpu/NER_DEVICE=mps/" .env
    echo "Создан .env, сгенерирован PROXY_API_TOKEN, NER_DEVICE=mps."
    printf 'Введите OPENROUTER_API_KEY (sk-or-v1-…) или Enter, чтобы задать позже: '
    read -r KEY
    if [ -n "$KEY" ]; then
        sed -i '' "s/OPENROUTER_API_KEY=sk-or-v1-REPLACE_WITH_YOUR_KEY/OPENROUTER_API_KEY=$KEY/" .env
        echo "Ключ OpenRouter записан."
    else
        echo "Ключ не задан — можно задать позже в .env."
    fi
fi

# ---------- 5. Прогрев моделей ----------
step "Прогрев NER-моделей (GLiNER + Natasha, первый раз — скачивание)"
"$PY" scripts/download_models.py || echo "[ПРЕДУПРЕЖДЕНИЕ] Прогрев завершился с ошибками — см. выше."

# ---------- 6. Самопроверка ----------
step "Самопроверка установки"
"$PY" scripts/install_selftest.py || echo "[ПРЕДУПРЕЖДЕНИЕ] Self-test FAIL — см. выше."

# ---------- Финал ----------
echo ""
echo "============================================================"
echo " Установка завершена!"
echo "============================================================"
echo "Запуск:        ./start_proxy.sh"
echo "Проверка:      http://127.0.0.1:8081/health"
echo ""
echo "Подключение Cline (расширение VS Code):"
echo "  Base URL:  http://127.0.0.1:8081/v1"
echo "  API Key:   любое значение (прокси его игнорирует)"
echo "  Model:     любое значение"
echo ""
echo "Не забудьте скопировать правила .clinerules в Cline."
