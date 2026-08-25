"""Определение аппаратного ускорителя машины.

Печатает ОДНО слово — flavor установки (extra из pyproject.toml):
  cuda     — Windows/Linux с NVIDIA GPU (nvidia-smi доступен);
  directml — Windows без NVIDIA (AMD/Intel GPU, NPU — через DirectML);
  cpu      — всё остальное (в т.ч. macOS: официальный onnxruntime-wheel
             собран только с CPUExecutionProvider; ускорение на M1–M4
             даёт PyTorch-фоллбэк через MPS, см. NER_DEVICE=mps в .env).

Используется установщиками install.ps1 / install.sh.
"""
from __future__ import annotations

import platform
import shutil
import subprocess


def detect() -> str:
    system = platform.system()
    if system == "Darwin":
        return "cpu"
    if shutil.which("nvidia-smi"):
        try:
            result = subprocess.run(
                ["nvidia-smi"],
                capture_output=True,
                timeout=10,
                check=False,
            )
            if result.returncode == 0:
                return "cuda"
        except (OSError, subprocess.SubprocessError):
            pass
    if system == "Windows":
        return "directml"
    return "cpu"


if __name__ == "__main__":
    print(detect())
