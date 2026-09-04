#!/bin/zsh
# ============================================================
#  Anonymizer Proxy — установщик LaunchAgent автозапуска (macOS)
#
#  Создаёт ~/Library/LaunchAgents/ru.anonymizer.proxy.plist:
#  сервер стартует при логине, перезапускается при падении
#  (KeepAlive), логи — в data/logs/.
#
#  Запуск:  bash install_launchagent.sh          — установить
#           bash install_launchagent.sh remove   — удалить
#
#  Лучшие практики launchd (по документации Apple):
#  - запускать исполняемый файл-обёртку (интерпретатор — в shebang),
#    а не интерпретатор+скрипт парой аргументов: иначе в «Login Items»
#    macOS приписывает интерпретатор, а не наш агент;
#  - LaunchAgent (пользовательский) вместо LaunchDaemon: серверу нужен
#    доступ к домашнему каталогу и он не требует sudo.
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"

LABEL="ru.anonymizer.proxy"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
PROJ_DIR="$(pwd -P)"
PY="$PROJ_DIR/.venv/bin/python"
WRAPPER="$PROJ_DIR/.venv/bin/anonymizer-proxy-launch"

if [ "${1:-}" = "remove" ]; then
    echo "Удаление LaunchAgent…"
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$PLIST" "$WRAPPER" 2>/dev/null || true
    echo "Готово: агент удалён (автозапуск отключён)."
    echo "Запуск вручную: ./start_proxy.sh"
    exit 0
fi

if [ ! -x "$PY" ]; then
    echo "[ОШИБКА] Не найден $PY"
    echo "Сначала выполните установку: bash install.sh"
    exit 1
fi

# ---------- 1. Скрипт запуска ----------
# launchd рекомендует запускать исполняемый файл-обёртку (интерпретатор —
# в shebang), а не интерпретатор+скрипт парой аргументов.
cat > "$WRAPPER" <<EOF
#!/bin/zsh
exec "$PY" -m anonymizer_proxy.main
EOF
chmod +x "$WRAPPER"

# ---------- 2. Плейлист агента ----------
mkdir -p "$HOME/Library/LaunchAgents" "$PROJ_DIR/data/logs"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${WRAPPER}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${PROJ_DIR}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${PROJ_DIR}/data/logs/proxy_launchagent.log</string>
    <key>StandardErrorPath</key>
    <string>${PROJ_DIR}/data/logs/proxy_launchagent.err.log</string>
</dict>
</plist>
EOF

# ---------- 3. Регистрация агента ----------
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "============================================================"
echo " LaunchAgent установлен: сервер стартует при логине"
echo " и перезапускается при падении (KeepAlive)."
echo "============================================================"
echo "Проверка:      http://127.0.0.1:8081/health (через ~30 с)"
echo "Остановка:     launchctl bootout gui/\$(id -u)/${LABEL}"
echo "Запуск снова:  launchctl bootstrap gui/\$(id -u) ~/Library/LaunchAgents/${LABEL}.plist"
echo "Полное удаление: bash install_launchagent.sh remove"

