#!/bin/bash
# ============================================================================
# Общий llama-рантайм машины: llama-server и GGUF-модели в ЕДИНОМ каталоге
# для всех проектов (anonymizer_proxy, hermes-disk-search и др.) — версия
# для macOS/Linux, аналог installers/ensure_llama_runtime.ps1.
#
# SYNC-COPY: файл предназначен быть идентичным в обоих репозиториях
# (hermes-disk-search/installers/ensure_llama_runtime.sh и
#  anonymizer_proxy/scripts/ensure_llama_runtime.sh) — как пара .ps1.
# При правке синхронизировать копии вручную.
#
# Каталог рантайма (те же пути вычисляет hds/llama_runtime.py):
#   $LLAMA_RUNTIME_DIR, иначе macOS — ~/Library/Application Support/llama-runtime,
#                              Linux — ~/.local/share/llama-runtime.
# Раскладка: bin/llama-server (+ dylib), models/<role>/*.gguf,
#            models/chat/current.json, projects.json, version.json.
#
# Бинарь: используется системный llama-server (Homebrew/MacPorts/PATH),
# он линкуется в bin/ рантайма (символическая ссылка — апгрейд llama.cpp
# через brew подхватывается сам). Если ни brew, ни бинаря нет — качается
# пре-билд llama.cpp с GitHub Releases (macos-arm64/x64) и снимается
# карантин Gatekeeper. На Apple Silicon Metal включён в сборке по умолчанию.
#
# Модели: chat (Qwen3.5-9B Q6_K), embedding (bge-m3 Q8_0), rerank
# (bge-reranker-v2-m3). Идемпотентно: уже скачанный файл не перекачивается,
# модель из старой установки LM Studio (~/.lmstudio/models) копируется.
#
# Примеры:
#   installers/ensure_llama_runtime.sh --models chat,embedding,rerank \
#       --project-name hermes-disk-search --project-root ~/hermes-disk-search \
#       --restart-args "-m hds.llama_server restart chat"
#   installers/ensure_llama_runtime.sh --models chat --force
# ============================================================================
set -u

RUNTIME_DIR="${LLAMA_RUNTIME_DIR:-}"
MODELS="chat"
FORCE=0
PROJECT_NAME=""
PROJECT_ROOT=""
RESTART_ARGS=""

usage() {
    cat <<'USAGE'
ensure_llama_runtime.sh — общий llama-рантайм машины (llama-server + GGUF)

  --models chat,embedding,rerank   роли моделей для скачивания (по умолчанию: chat)
  --project-name NAME              зарегистрировать проект в projects.json
  --project-root PATH              корень проекта (вместе с --project-name)
  --restart-args "ARGS"            аргументы перезапуска llama-инстанса проекта
                                   (напр. "-m hds.llama_server restart chat")
  --runtime-dir PATH               переопределить каталог рантайма
  --force                          переустановить бинарь и модели
  -h, --help                       эта справка
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --models)       MODELS="${2:-}"; shift 2 2>/dev/null || shift ;;
        --project-name) PROJECT_NAME="${2:-}"; shift 2 2>/dev/null || shift ;;
        --project-root) PROJECT_ROOT="${2:-}"; shift 2 2>/dev/null || shift ;;
        --restart-args) RESTART_ARGS="${2:-}"; shift 2 2>/dev/null || shift ;;
        --runtime-dir)  RUNTIME_DIR="${2:-}"; shift 2 2>/dev/null || shift ;;
        --force|-f)     FORCE=1; shift ;;
        -h|--help)      usage; exit 0 ;;
        *) echo "Неизвестный аргумент: $1" >&2; usage >&2; exit 2 ;;
    esac
done

have() { command -v "$1" >/dev/null 2>&1; }
ok()   { echo "[ok] $*"; }
info() { echo "[..] $*"; }
dim()  { echo "[--] $*"; }
warn() { echo "[!!] $*" >&2; }

filesize() {   # байты файла (0 — файла нет): BSD stat (macOS) и GNU stat (Linux)
    [ -e "$1" ] || { echo 0; return; }
    stat -f%z "$1" 2>/dev/null || stat -c%s "$1" 2>/dev/null || echo 0
}

# ---------- 1. Каталог рантайма и вариант сборки ----------
if [ -z "$RUNTIME_DIR" ]; then
    case "$(uname -s)" in
        Darwin) RUNTIME_DIR="$HOME/Library/Application Support/llama-runtime" ;;
        *)      RUNTIME_DIR="$HOME/.local/share/llama-runtime" ;;
    esac
fi
case "$(uname -s)" in
    Darwin) VARIANT="metal" ;;   # Metal есть и на Apple Silicon, и на Intel Mac
    *)      VARIANT="cpu" ;;
esac
BIN_DIR="$RUNTIME_DIR/bin"
LLAMA="$BIN_DIR/llama-server"
VERSION_FILE="$RUNTIME_DIR/version.json"
MANIFEST="$RUNTIME_DIR/models/chat/current.json"
mkdir -p "$BIN_DIR" "$RUNTIME_DIR/models/chat"
info "Общий llama-рантайм: $RUNTIME_DIR"

write_version_json() {   # variant source
    cat > "$VERSION_FILE" <<EOF
{
  "variant": "$1",
  "source": "$2",
  "installed_at": "$(date +%Y-%m-%dT%H:%M:%S)"
}
EOF
}

have_variant=""
if [ -f "$VERSION_FILE" ]; then
    have_variant="$(sed -n 's/.*"variant"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
        "$VERSION_FILE" | head -1)"
fi

# ---------- 2. Бинарь llama-server ----------
find_system_llama() {   # системный llama-server (brew/macports/PATH) или ничего
    for cand in "$(command -v llama-server 2>/dev/null || true)" \
                /opt/homebrew/bin/llama-server \
                /usr/local/bin/llama-server \
                /opt/local/bin/llama-server; do
        # сам рантайм (или его же файл под другим путём) не годится в
        # источники: ссылка на себя превратилась бы в битую
        if [ -n "$cand" ] && [ -x "$cand" ] &&
                [ "$cand" != "$LLAMA" ] && ! [ "$cand" -ef "$LLAMA" ]; then
            echo "$cand"
            return 0
        fi
    done
    return 1
}

prebuilt_asset() {   # os arch -> две строки: tag, url (GitHub Releases llama.cpp)
    python3 - "$1" "$2" <<'PYEOF'
import json, sys, urllib.request
os_name, arch = sys.argv[1], sys.argv[2]
url = "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=10"
req = urllib.request.Request(url, headers={"User-Agent": "ensure-llama-runtime"})
need = "-bin-%s-%s" % (os_name, arch)
for rel in json.load(urllib.request.urlopen(req, timeout=30)):
    for asset in rel.get("assets") or []:
        name = asset.get("name", "")
        if need in name and name.endswith(".zip"):
            print(rel.get("tag_name", ""))
            print(asset.get("browser_download_url", ""))
            sys.exit(0)
sys.exit(1)
PYEOF
}

download_prebuilt() {
    local os_name arch found tag url tmp zip top
    case "$(uname -s)" in
        Darwin) os_name="macos" ;;
        *)      os_name="linux" ;;
    esac
    case "$(uname -m)" in
        arm64|aarch64) arch="arm64" ;;
        x86_64|amd64)  arch="x64" ;;
        *) warn "Неизвестная архитектура $(uname -m) — скачайте llama.cpp с https://github.com/ggml-org/llama.cpp/releases и распакуйте в $BIN_DIR"
           return 1 ;;
    esac
    if ! have python3 || ! have curl; then
        warn "Нужны python3 и curl — скачайте llama.cpp вручную с https://github.com/ggml-org/llama.cpp/releases и распакуйте в $BIN_DIR"
        return 1
    fi
    info "Скачиваю пре-билд llama.cpp (bin-$os_name-$arch) с GitHub Releases..."
    found="$(prebuilt_asset "$os_name" "$arch")" || {
        warn "В последних релизах llama.cpp нет ассета -bin-$os_name-$arch; скачайте вручную: https://github.com/ggml-org/llama.cpp/releases"
        return 1; }
    tag="$(printf '%s\n' "$found" | sed -n 1p)"
    url="$(printf '%s\n' "$found" | sed -n 2p)"
    tmp="$(mktemp -d "${TMPDIR:-/tmp}/llama-runtime.XXXXXX")" || return 1
    zip="$tmp/llama.zip"
    if ! curl -L --fail --progress-bar -o "$zip" "$url"; then
        warn "Не удалось скачать $url"
        rm -rf "$tmp"
        return 1
    fi
    if ! have unzip; then
        warn "unzip не найден — распакуйте $zip в $BIN_DIR вручную"
        rm -rf "$tmp"
        return 1
    fi
    unzip -q -o "$zip" -d "$BIN_DIR" || { warn "unzip: ошибка распаковки $zip"; rm -rf "$tmp"; return 1; }
    rm -rf "$tmp"
    # llama.cpp распаковывается в подпапку llama-<tag>-bin-…/ — поднимаем файлы
    top="$(find "$BIN_DIR" -mindepth 1 -maxdepth 1 -type d | head -1)"
    if [ -n "$top" ]; then
        mv -f "$top"/* "$BIN_DIR/" 2>/dev/null
        rm -rf "$top"
    fi
    chmod +x "$LLAMA" 2>/dev/null
    # Gatekeeper: скачанный бинарь несёт метку com.apple.quarantine — без её
    # снятия macOS завершит процесс («killed»/«повреждён»)
    if have xattr; then xattr -dr com.apple.quarantine "$BIN_DIR" 2>/dev/null; fi
    write_version_json "$VARIANT" "prebuilt:$tag"
    ok "llama-server установлен ($VARIANT, $tag): $LLAMA"
}

bin_ok=0
if [ "$FORCE" -eq 0 ] && [ -x "$LLAMA" ] && \
        { [ -z "$have_variant" ] || [ "$have_variant" = "$VARIANT" ]; }; then
    bin_ok=1
fi
if [ "$bin_ok" -eq 1 ]; then
    ok "llama-server уже установлен ($VARIANT): $LLAMA"
else
    src="$(find_system_llama || true)"
    if [ -z "$src" ] && have brew; then
        info "llama.cpp не найден — ставлю через Homebrew (Metal на Apple Silicon включается сам)..."
        brew install llama.cpp || warn "brew install llama.cpp завершился ошибкой"
        src="$(find_system_llama || true)"
    fi
    if [ -n "$src" ]; then
        rm -f "$LLAMA"
        ln -s "$src" "$LLAMA" || { warn "Не удалось создать ссылку $LLAMA -> $src"; }
        write_version_json "$VARIANT" "system:$src"
        ok "llama-server подключён из системы ($src): $LLAMA"
    else
        download_prebuilt || true
    fi
fi
if [ -x "$LLAMA" ]; then
    ver="$("$LLAMA" --version 2>&1 | head -1)"
    if [ -n "$ver" ]; then dim "llama-server: $ver"; else warn "llama-server --version не ответил — проверьте бинарь $LLAMA"; fi
fi

# ---------- 3. GGUF-модели (общий каталог models/<role>/) ----------
# Пресеты: роль → файл/URL/минимальный размер. Смена дефолтной чат-модели —
# правка здесь + DEFAULT_CHAT в llama_runtime.py (обе копии) + таблица в
# ensure_llama_runtime.ps1.
preset_field() {   # role field -> значение (ошибка, если роли нет)
    case "$1:$2" in
        chat:file)       echo "Qwen3.5-9B-Q6_K.gguf" ;;
        chat:url)        echo "https://huggingface.co/unsloth/Qwen3.5-9B-GGUF/resolve/main/Qwen3.5-9B-Q6_K.gguf" ;;
        chat:minmb)      echo 4000 ;;
        embedding:file)  echo "bge-m3-Q8_0.gguf" ;;
        embedding:url)   echo "https://huggingface.co/lm-kit/bge-m3-gguf/resolve/main/bge-m3-Q8_0.gguf" ;;
        embedding:minmb) echo 300 ;;
        rerank:file)     echo "bge-reranker-v2-m3-q8_0.gguf" ;;
        rerank:url)      echo "https://huggingface.co/klnstpr/bge-reranker-v2-m3-Q8_0-GGUF/resolve/main/bge-reranker-v2-m3-q8_0.gguf" ;;
        rerank:minmb)    echo 300 ;;
        *) return 1 ;;
    esac
}

download_model() {   # role
    local role="$1" file url minmb dir dest old rc
    if ! file="$(preset_field "$role" file)"; then
        dim "Неизвестная роль модели: $role"
        return 0
    fi
    url="$(preset_field "$role" url)"
    minmb="$(preset_field "$role" minmb)"
    dir="$RUNTIME_DIR/models/$role"
    dest="$dir/$file"
    mkdir -p "$dir"
    if [ "$FORCE" -eq 0 ] && [ "$(filesize "$dest")" -gt $((minmb * 1024 * 1024)) ]; then
        ok "$role : $file уже на месте"
        return 0
    fi
    # Быстрый путь: модель уже скачана в старой установке LM Studio
    # (~/.lmstudio) — копируем, а не тянем повторно из сети.
    if [ -d "$HOME/.lmstudio/models" ]; then
        old="$(find "$HOME/.lmstudio/models" -type f -name "$file" \
               -size +${minmb}M 2>/dev/null | head -1)"
        if [ -n "$old" ]; then
            info "Найдена модель из LM Studio: $old — копирую"
            if cp "$old" "$dest"; then ok "Скопировано: $dest"; return 0; fi
        fi
    fi
    info "Скачиваю $role : $file (~${minmb} МБ+)..."
    dim "     $url"
    rc=1
    if [ -n "${HF_TOKEN:-}" ]; then
        curl -L --fail --progress-bar -H "Authorization: Bearer $HF_TOKEN" \
            -o "$dest.part" "$url" && rc=0
    else
        curl -L --fail --progress-bar -o "$dest.part" "$url" && rc=0
    fi
    if [ "$rc" -eq 0 ] && [ "$(filesize "$dest.part")" -gt $((minmb * 1024 * 1024)) ]; then
        mv -f "$dest.part" "$dest"
        ok "Скачано: $dest"
        return 0
    fi
    rm -f "$dest.part"
    warn "Не удалось скачать $file. Скачайте вручную:"
    warn "     $url  ->  $dest"
    return 1
}

# роли через запятую: --models chat,embedding,rerank (словоделение — намеренное)
for role in $(printf '%s' "$MODELS" | tr ',' ' '); do
    download_model "$role"
done

# ---------- 4. Манифест активной чат-модели ----------
# current.json задаёт модель для спецификатора "shared:chat" во всех проектах:
# смена файла = смена общей чат-модели сразу у всех.
if [ ! -f "$MANIFEST" ]; then
    chat_file=""
    if [ -f "$RUNTIME_DIR/models/chat/Qwen3.5-9B-Q6_K.gguf" ]; then
        chat_file="Qwen3.5-9B-Q6_K.gguf"          # = DEFAULT_CHAT в llama_runtime.py
    else
        chat_file="$( (cd "$RUNTIME_DIR/models/chat" && ls -1 *.gguf) 2>/dev/null | head -1)"
    fi
    if [ -n "$chat_file" ]; then
        printf '{\n  "file": "%s",\n  "switched_at": "%s"\n}\n' \
            "$chat_file" "$(date +%Y-%m-%dT%H:%M:%S)" > "$MANIFEST"
        ok "Активная чат-модель: $chat_file (models/chat/current.json)"
    fi
fi

# ---------- 5. Регистрация проекта (для синхронной смены модели) ----------
if [ -n "$PROJECT_NAME" ] && [ -n "$PROJECT_ROOT" ]; then
    if have python3; then
        python3 - "$RUNTIME_DIR/projects.json" "$PROJECT_NAME" "$PROJECT_ROOT" \
                  "$RESTART_ARGS" <<'PYEOF'
import json, os, sys

path, name, root, args_str = sys.argv[1:5]
root = os.path.realpath(root)
try:
    with open(path, encoding="utf-8-sig") as f:
        data = json.load(f)
except Exception:              # нет/битый файл — начинаем с пустого реестра
    data = {}
items = data.get("projects") if isinstance(data, dict) else data
items = [p for p in (items or []) if isinstance(p, dict) and p.get("root")]
items = [p for p in items
         if p.get("name") != name and os.path.realpath(p.get("root", "")) != root]
items.append({"name": name, "root": root,
              "restart_args": args_str.split() if args_str else []})
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump({"projects": items}, f, ensure_ascii=False, indent=2)
os.replace(tmp, path)
print("[ok] Проект зарегистрирован в рантайме: %s (%s)" % (name, root))
PYEOF
    else
        warn "python3 не найден — проект не зарегистрирован: смена общей чат-модели"
        warn "не перезапустит его llama-инстанс (см. projects.json в рантайме)"
    fi
fi

echo ""
ok "Общий llama-рантайм готов: $RUNTIME_DIR"
dim "  бинарь:   $LLAMA"
dim "  модели:   $RUNTIME_DIR/models/{chat,embedding,rerank}"
dim "  проверка: python -m hds.llama_runtime list"
dim "            python -m anonymizer_proxy.llama_runtime list"
dim "  запуск серверов проекта: python -m hds.llama_server start"
dim "  постоянно в памяти (macOS): python -m hds.llama_server run <роль> —"
dim "  foreground-процесс для LaunchAgent; инстансы, поднятые через start,"
dim "  переживают перезапуск UI и терминала (отвязанные процессы)."