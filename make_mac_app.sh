#!/bin/zsh
# ============================================================
#  Anonymizer Proxy — генератор приложения macOS (make_mac_app.sh)
#
#  Создаёт «Anonymizer Proxy.app» рядом с проектом: двойной клик
#  (или запуск из Launchpad/Dock) открывает окно Терминала с
#  сервером — аналог ярлыка «Anonymizer Proxy» в меню Пуск Windows.
#
#  Запуск:  bash make_mac_app.sh          — создать/обновить .app
#           bash make_mac_app.sh remove   — удалить .app
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"

PROJ_DIR="$(pwd -P)"
APP_DIR="$PROJ_DIR/Anonymizer Proxy.app"
BIN_DIR="$APP_DIR/Contents/MacOS"
RES_DIR="$APP_DIR/Contents/Resources"

if [ "${1:-}" = "remove" ]; then
    echo "Удаление приложения…"
    rm -rf "$APP_DIR"
    echo "Готово: Anonymizer Proxy.app удалён."
    exit 0
fi

if [ ! -x ".venv/bin/python" ]; then
    echo "[ОШИБКА] Не найден .venv/bin/python"
    echo "Сначала выполните установку: bash install.sh"
    exit 1
fi

# ---------- 1. Каркас .app ----------
rm -rf "$APP_DIR"
mkdir -p "$BIN_DIR" "$RES_DIR"

# ---------- 2. Info.plist ----------
cat > "$APP_DIR/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>Anonymizer Proxy</string>
    <key>CFBundleDisplayName</key>
    <string>Anonymizer Proxy</string>
    <key>CFBundleIdentifier</key>
    <string>ru.anonymizer.proxy.app</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleExecutable</key>
    <string>AnonymizerProxy</string>
    <key>LSUIElement</key>
    <false/>
    <key>NSHighResolutionCapable</key>
    <true/>
</dict>
</plist>
EOF

# ---------- 3. Исполняемый лаунчер ----------
# Открывает окно Терминала со start_proxy.command — видимые логи сервера,
# страница настроек в браузере (открывается самим .command-файлом),
# остановка Ctrl+C (как у ярлыка в Windows). Пока .app запущен, его иконка
# висит в Dock — визуальный индикатор «сервер включён».
cat > "$BIN_DIR/AnonymizerProxy" <<EOF
#!/bin/zsh
open -a Terminal "$PROJ_DIR/start_proxy.command"
EOF
chmod +x "$BIN_DIR/AnonymizerProxy"

# ---------- 4. Иконка (если есть) ----------
# Кастомный значок: положите icon.icns в Resources (необязательно).
if [ -f "$RES_DIR/icon.icns" ]; then
    /usr/libexec/PlistBuddy -c \
        "Add :CFBundleIconFile string icon" "$APP_DIR/Contents/Info.plist" \
        2>/dev/null || true
fi

echo "============================================================"
echo " Создано: $APP_DIR"
echo "============================================================"
echo "Как пользоваться:"
echo "  1. Двойной клик по Anonymizer Proxy.app — откроется окно"
echo "     Терминала с логами сервера (остановка: Ctrl+C) и страница"
echo "     настроек в браузере по умолчанию."
echo "  2. Перетащите .app в «Программы» или в Dock — запуск"
echo "     станет доступен оттуда одним кликом."
echo ""
echo "Автозапуск при логине (по желанию): bash install_launchagent.sh"
