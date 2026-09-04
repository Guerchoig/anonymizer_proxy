#!/bin/zsh
# ============================================================
#  Anonymizer Proxy — запуск двойным кликом из Finder (macOS)
#  Finder открывает .command-файлы в Терминале; скрипт
#  запускается из своего каталога независимо от CWD.
# ============================================================
cd "$(dirname "$0")" || exit 1

PY=".venv/bin/python"
if [ ! -x "$PY" ]; then
    echo "[ОШИБКА] Не найден .venv/bin/python"
    echo "Сначала выполните установку: bash install.sh"
    echo "(Нажмите Enter, чтобы закрыть окно)"
    read -r _
    exit 1
fi

echo "Запуск Anonymizer Proxy…"
echo "Остановка: Ctrl+C в этом окне (сервер завершится вместе с ним)."
exec "$PY" -m anonymizer_proxy.main "$@"
