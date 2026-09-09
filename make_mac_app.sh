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

# ---------- 4. Иконка ----------
# Из icon.png (корень проекта) генерируется Resources/app.icns штатными
# утилитами macOS (sips + iconutil); размеры больше исходника пропускаются.
# Если icon.png нет — поддержан ручной вариант: готовый icon.icns в
# Resources. Без иконки .app получает стандартный значок системы.
if [ -f "$RES_DIR/icon.icns" ]; then
    PLIST_ICON_SET=1
elif [ -f "$PROJ_DIR/icon.png" ] && command -v sips >/dev/null 2>&1; then
    ICONSET="$(mktemp -d)/app.iconset"
    mkdir -p "$ICONSET"
    W=$(sips -g pixelWidth "$PROJ_DIR/icon.png" | awk '/pixelWidth/{print $2}')
    for pair in "16 icon_16x16.png" "32 icon_16x16@2x.png" \
                "32 icon_32x32.png" "64 icon_32x32@2x.png" \
                "128 icon_128x128.png" "256 icon_128x128@2x.png" \
                "256 icon_256x256.png"; do
        size=${pair%% *}
        name=${pair#* }
        [ "$size" -le "$W" ] || continue
        sips -z "$size" "$size" "$PROJ_DIR/icon.png" \
            --out "$ICONSET/$name" >/dev/null
    done
    if iconutil -c icns "$ICONSET" -o "$RES_DIR/app.icns" 2>/dev/null; then
        PLIST_ICON_SET=1
    fi
    rm -rf "$(dirname "$ICONSET")"
fi
if [ "${PLIST_ICON_SET:-0}" = "1" ]; then
    /usr/libexec/PlistBuddy -c \
        "Add :CFBundleIconFile string app" "$APP_DIR/Contents/Info.plist" \
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
