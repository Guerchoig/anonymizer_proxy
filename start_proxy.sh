#!/usr/bin/env bash
# Запуск прокси-сервера анонимизации ТОЛЬКО через .venv (macOS/Linux).
# Аналог start_proxy.cmd: запуск системным python приводит к ошибке
# «NER-модель не загрузилась», т.к. зависимости стоят только в venv.
cd "$(dirname "$0")" || exit 1

PY=".venv/bin/python"
if [ ! -x "$PY" ]; then
    echo "[ОШИБКА] Не найден $PY"
    echo "Установите окружение: ./install.sh"
    exit 1
fi

exec "$PY" -m anonymizer_proxy.main "$@"
