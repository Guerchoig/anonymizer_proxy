#!/bin/zsh
# ============================================================
#  Anonymizer Proxy — установщик LaunchAgents автозапуска (macOS)
#
#  Создаёт ~/Library/LaunchAgents/:
#    ru.anonymizer.proxy — прокси-сервер анонимизации
#    ru.anonymizer.llama — llama-server (локальная LLM, порт 8080)
#
#  Режимы (первый аргумент): proxy | llama | all (по умолчанию proxy)
#  Запуск:  bash install_launchagent.sh            — прокси
#           bash install_launchagent.sh llama      — llama-server
#           bash install_launchagent.sh all        — оба
#           bash install_launchagent.sh remove     — удалить оба
#
#  llama-agent запускает `llama-server run` в foreground с KeepAlive:
#  сервер стартует при логине, перезапускается при падении и держит
#  модель в памяти независимо от прокси (доступ другим приложениям).
#
#  Лучшие практики launchd (по документации Apple):
#  - запускать исполняемый файл-обёртку (интерпретатор — в shebang);
#  - LaunchAgent (пользовательский) вместо LaunchDaemon: без sudo.
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"

MODE="${1:-proxy}"
LABEL="ru.anonymizer.proxy"
LABEL_LLAMA="ru.anonymizer.llama"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
PLIST_LLAMA="$HOME/Library/LaunchAgents/${LABEL_LLAMA}.plist"
PROJ_DIR="$(pwd -P)"
PY="$PROJ_DIR/.venv/bin/python"
WRAPPER="$PROJ_DIR/.venv/bin/anonymizer-proxy-launch"
WRAPPER_LLAMA="$PROJ_DIR/.venv/bin/anonymizer-llama-launch"

if [ "$MODE" = "remove" ]; then
    echo "Удаление LaunchAgents…"
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    launchctl bootout "gui/$(id -u)/$LABEL_LLAMA" 2>/dev/null || true
    rm -f "$PLIST" "$PLIST_LLAMA" "$WRAPPER" "$WRAPPER_LLAMA" 2>/dev/null || true
    echo "Готово: агенты удалены (автозапуск отключён)."
    echo "Запуск вручную: ./start_proxy.sh"
    exit 0
fi

if [ ! -x "$PY" ]; then
    echo "[ОШИБКА] Не найден $PY"
    echo "Сначала выполните установку: bash install.sh"
    exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$PROJ_DIR/data/logs"

if [ "$MODE" = "proxy" ] || [ "$MODE" = "all" ]; then
    # ---------- Скрипт запуска прокси ----------
    # launchd рекомендует исполняемый файл-обёртку (интерпретатор — в shebang),
    # а не интерпретатор+скрипт парой аргументов.
    cat > "$WRAPPER" <<EOF
#!/bin/zsh
exec "$PY" -m anonymizer_proxy.main
EOF
    chmod +x "$WRAPPER"

    # ---------- Плейлист агента прокси ----------
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
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    echo "LaunchAgent прокси установлен (ru.anonymizer.proxy)."
fi

if [ "$MODE" = "llama" ] || [ "$MODE" = "all" ]; then
    # ---------- Скрипт запуска llama-server ----------
    # `llm_server run` строит команду из LLM_SERVER_* и exec-ит llama-server
    # в foreground — KeepAlive перезапускает его при падении.
    cat > "$WRAPPER_LLAMA" <<EOF
#!/bin/zsh
exec "$PY" -m anonymizer_proxy.llm_server run
EOF
    chmod +x "$WRAPPER_LLAMA"

    cat > "$PLIST_LLAMA" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL_LLAMA}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${WRAPPER_LLAMA}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${PROJ_DIR}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${PROJ_DIR}/data/logs/llama_launchagent.log</string>
    <key>StandardErrorPath</key>
    <string>${PROJ_DIR}/data/logs/llama_launchagent.err.log</string>
</dict>
</plist>
EOF
    launchctl bootout "gui/$(id -u)/$LABEL_LLAMA" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST_LLAMA"
    echo "LaunchAgent llama-server установлен (ru.anonymizer.llama)."
fi

echo "============================================================"
echo " Автозапуск включён: серверы стартуют при логине и"
echo " перезапускаются при падении (KeepAlive)."
echo "============================================================"
echo "Проверка:      http://127.0.0.1:8081/health (прокси, ~30 с)"
echo "               http://127.0.0.1:8080/health (llama-server)"
echo "Остановка:     launchctl bootout gui/\$(id -u)/${LABEL}"
echo "               launchctl bootout gui/\$(id -u)/${LABEL_LLAMA}"
echo "Полное удаление: bash install_launchagent.sh remove"