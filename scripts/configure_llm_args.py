#!/usr/bin/env python
"""Подбор GPU-аргументов llama-server под железо и запись в .env.

Вызывается установщиками install.ps1 (Windows) / install.sh (macOS) после
шага «общий llama-рантайм». Без подбора llama-server запускается с дефолтом
llama.cpp (-ngl 0 — вся модель на CPU): на машине с GPU ответы в разы
медленнее, а CPU занят целиком (наблюдение: «GPU слабо нагружен, CPU — на
максимум»).

Логика по платформам:
- Windows + NVIDIA (nvidia-smi):
    VRAM вмещает веса модели + запас на KV-кэш -> -ngl 99 (полная выгрузка);
    вмещает заметную часть -> -ngl 16 (частичная выгрузка);
    иначе -> -ngl 0 (только CPU).
- macOS (Apple Silicon): llama.cpp собран с Metal и по умолчанию грузит все
  слои на GPU (unified memory) — флаг -ngl не пишем вовсе.
- Всё прочее (Windows без NVIDIA, Linux без NVIDIA): -ngl 0.

Размер GGUF берётся из активной чат-модели общего llama-рантайма
(models/chat/current.json -> path) или из LLM_SERVER_MODEL (если это путь
к файлу). Правка .env идемпотентна: если пользователь сам задал -ngl /
--n-gpu-layers в LLM_SERVER_EXTRA_ARGS — значение НЕ перезаписывается
(кроме режима --force). Запуск: python scripts/configure_llm_args.py [--force]
"""
from __future__ import annotations

import argparse
import json
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"

_NGL_RE = re.compile(r"(--n-gpu-layers|-ngl)(\s*=?\s*\d+)?", re.IGNORECASE)


# ==================== Детект железа ====================

def detect_nvidia_vram_mib() -> int:
    """Суммарная VRAM NVIDIA GPU (МиБ) или 0, если NVIDIA нет/недоступен."""
    smi = shutil.which("nvidia-smi")
    if not smi:
        return 0
    try:
        out = subprocess.run(
            [smi, "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15, check=False,
        ).stdout
        values = [int(v.strip()) for v in out.splitlines() if v.strip().isdigit()]
        return sum(values)
    except (OSError, subprocess.SubprocessError, ValueError):
        return 0


def detect_platform() -> str:
    """nvidia | apple | cpu"""
    if detect_nvidia_vram_mib() > 0:
        return "nvidia"
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        return "apple"  # Apple Silicon: llama.cpp с Metal, unified memory
    return "cpu"


# ==================== Активная чат-модель ====================

def llama_runtime_root() -> Path | None:
    """Каталог общего llama-рантайма (тот же приоритет, что в llama_runtime.py)."""
    import os
    env_dir = os.getenv("LLAMA_RUNTIME_DIR", "").strip()
    if env_dir:
        return Path(env_dir)
    if platform.system() == "Windows":
        base = os.getenv("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "llama-runtime"
    return Path.home() / "Library" / "Application Support" / "llama-runtime"


def read_env_value(key: str) -> str:
    """Значение KEY из .env проекта (без dotenv — standalone-скрипт)."""
    if not ENV_PATH.is_file():
        return ""
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip().strip('"')
    return ""


def find_chat_gguf() -> Path | None:
    """Файл GGUF активной чат-модели (для оценки потребности в VRAM)."""
    spec = read_env_value("LLM_SERVER_MODEL") or "shared:chat"
    if spec and not spec.lower().startswith("shared:"):
        p = Path(spec)
        return p if p.is_file() else None
    root = llama_runtime_root()
    if root is None:
        return None
    manifest = root / "models" / "chat" / "current.json"
    if not manifest.is_file():
        return None
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    # Путь в манифесте — относительно каталога манифеста (models/chat)
    for key in ("path", "file", "gguf"):
        raw = str(data.get(key) or "")
        if not raw:
            continue
        p = Path(raw)
        if not p.is_absolute():
            p = manifest.parent / raw
        if p.is_file():
            return p
    return None


# ==================== Выбор -ngl ====================

def choose_ngl(kind: str, vram_mib: int, model_mib: int) -> int | None:
    """Число GPU-слоёв; None — флаг не нужен (Metal сам всё делает)."""
    if kind == "apple":
        return None
    if kind == "nvidia" and model_mib > 0:
        # Запас 2 ГиБ: KV-кэш q8_0 при ctx 32K (~1 ГиБ) + буферы/вычисления
        need_mib = model_mib + 2048
        if need_mib <= vram_mib * 0.9:
            return 99
        if model_mib * 0.35 <= vram_mib * 0.8:
            return 16  # частичная выгрузка: заметно быстрее CPU-only
        return 0
    return 0


# ==================== Правка .env ====================

def update_env(ngl: int | None, force: bool = False) -> str:
    """Добавить/обновить -ngl в LLM_SERVER_EXTRA_ARGS .env (идемпотентно).

    Возвращает человекочитаемый итог действий.
    """
    if ngl is None:
        return ("macOS (Metal): флаг -ngl не требуется — llama.cpp сам "
                "выгружает все слои на GPU; .env не изменён")

    if not ENV_PATH.is_file():
        return ".env не найден (установщик ещё не создавал конфигурацию) — пропускаю"

    text = ENV_PATH.read_text(encoding="utf-8")
    m = re.search(r"^LLM_SERVER_EXTRA_ARGS=(.*)$", text, re.MULTILINE)
    if m is None:
        text = text.rstrip("\n") + f"\nLLM_SERVER_EXTRA_ARGS=-ngl {ngl}\n"
        ENV_PATH.write_text(text, encoding="utf-8")
        return f".env: добавлена LLM_SERVER_EXTRA_ARGS=-ngl {ngl}"

    existing = m.group(1).strip()
    user_ngl = _NGL_RE.search(existing)
    if not force and user_ngl:
        return (f".env: пользовательский {user_ngl.group(0)!r} в "
                f"LLM_SERVER_EXTRA_ARGS не трогаю (перезаписать: --force)")

    stripped = _NGL_RE.sub("", existing).strip()
    new_value = (stripped + f" -ngl {ngl}").strip()
    if new_value == existing:
        return f".env: -ngl {ngl} уже выставлен — изменений нет"
    new_text = text[:m.start(1)] + new_value + text[m.end(1):]
    ENV_PATH.write_text(new_text, encoding="utf-8")
    return f".env: LLM_SERVER_EXTRA_ARGS обновлены -> {new_value!r}"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Подбор GPU-аргументов llama-server (-ngl) под железо")
    parser.add_argument("--force", action="store_true",
                        help="перезаписать пользовательский -ngl в .env")
    parser.add_argument("--dry-run", action="store_true",
                        help="только показать выбор, .env не менять")
    args = parser.parse_args()

    kind = detect_platform()
    vram = detect_nvidia_vram_mib()
    gguf = find_chat_gguf()
    model_mib = int(gguf.stat().st_size / (1024 * 1024)) if gguf else 0

    if kind == "nvidia":
        print(f"GPU: NVIDIA, VRAM {vram} МиБ")
    elif kind == "apple":
        print("Платформа: Apple Silicon (Metal)")
    else:
        print("GPU с CUDA не обнаружен — llama-server будет работать на CPU")

    if gguf:
        print(f"Модель: {gguf.name} ({model_mib} МиБ)")
    else:
        print("Активная GGUF-модель не найдена (рантайм ещё не установлен) — "
              "-ngl выберу консервативно")

    ngl = choose_ngl(kind, vram, model_mib)
    if ngl is None:
        print("Выбор: без -ngl (Metal выгружает слои по умолчанию)")
    else:
        print(f"Выбор: -ngl {ngl}")

    if args.dry_run:
        return 0
    print(update_env(ngl, force=args.force))
    return 0


if __name__ == "__main__":
    sys.exit(main())