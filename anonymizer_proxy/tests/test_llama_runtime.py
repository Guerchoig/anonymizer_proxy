"""
Тесты общего llama-рантайма (llama_runtime): пути, манифест активной
чат-модели, разрешение спецификатора shared:<role>, реестр проектов,
смена модели без перезапуска.

Сеть и реальные llama-инстансы не используются: каталог рантайма
подменяется временным через LLAMA_RUNTIME_DIR.

Запуск: python -m anonymizer_proxy.tests.test_llama_runtime
"""
import json
import os
import sys
import tempfile
from pathlib import Path

from anonymizer_proxy import llama_runtime as lr


def test_paths_follow_env():
    """Пути рантайма вычисляются от LLAMA_RUNTIME_DIR."""
    with tempfile.TemporaryDirectory() as td:
        old = os.environ.get(lr.RUNTIME_DIR_ENV)
        os.environ[lr.RUNTIME_DIR_ENV] = td
        try:
            assert Path(lr.runtime_dir()) == Path(td)
            assert lr.bin_dir() == Path(td) / "bin"
            assert lr.models_dir("chat") == Path(td) / "models" / "chat"
            assert lr.find_binary() == ""          # бинаря нет
            (Path(td) / "bin").mkdir(parents=True)
            exe = lr.bin_dir() / ("llama-server.exe" if sys.platform == "win32"
                                  else "llama-server")
            exe.write_bytes(b"x")
            assert lr.find_binary() == str(exe)    # бинарь найден
        finally:
            if old is None:
                os.environ.pop(lr.RUNTIME_DIR_ENV, None)
            else:
                os.environ[lr.RUNTIME_DIR_ENV] = old
    print("TEST 1 OK: пути и бинарь общего рантайма (LLAMA_RUNTIME_DIR)")


def test_manifest_and_resolve(td: Path):
    """Манифест: set_current_chat → resolve_model('shared:chat')."""
    d = lr.models_dir("chat")
    d.mkdir(parents=True, exist_ok=True)
    (d / "a.gguf").write_bytes(b"a")
    (d / "b.gguf").write_bytes(b"b")

    # без манифеста при двух моделях — shared:chat не разрешается
    try:
        lr.resolve_model("shared:chat")
        raise AssertionError("ожидали FileNotFoundError")
    except FileNotFoundError as exc:
        assert "current.json" in str(exc)

    lr.set_current_chat("a.gguf")
    assert lr.read_current("chat")["file"] == "a.gguf"
    assert lr.resolve_model("shared:chat") == d / "a.gguf"
    assert lr.resolve_model("shared") == d / "a.gguf"   # роль по умолчанию

    # смена активной модели — просто другой файл в манифесте
    lr.set_current_chat("b.gguf")
    assert lr.resolve_model("shared:chat") == d / "b.gguf"

    # обычный путь не трогаем
    assert lr.resolve_model(r"C:\tmp\x.gguf") == Path(r"C:\tmp\x.gguf")

    # манифест на несуществующий файл — понятная ошибка
    lr.set_current_chat("a.gguf")
    (d / "a.gguf").unlink()
    try:
        lr.resolve_model("shared:chat")
        raise AssertionError("ожидали FileNotFoundError")
    except FileNotFoundError as exc:
        assert "a.gguf" in str(exc)

    # set_current_chat на отсутствующий файл — отказ
    try:
        lr.set_current_chat("nope.gguf")
        raise AssertionError("ожидали FileNotFoundError")
    except FileNotFoundError:
        pass
    print("TEST 2 OK: манифест current.json и resolve_model('shared:chat')")


def test_single_model_without_manifest(td: Path):
    """Единственная модель роли = активная (манифеста нет)."""
    d = lr.models_dir("embedding")
    d.mkdir(parents=True, exist_ok=True)
    (d / "only.gguf").write_bytes(b"x")
    assert lr.resolve_model("shared:embedding") == d / "only.gguf"
    print("TEST 3 OK: одна модель в каталоге роли — активная без манифеста")


def test_registry_and_switch(td: Path):
    """Реестр проектов и switch_chat_model без перезапуска инстансов."""
    lr.register_project("proj-a", str(td / "a"), ["-m", "pkg.a", "restart"])
    lr.register_project("proj-b", str(td / "b"), ["-m", "pkg.b", "restart"])
    lr.register_project("proj-a", str(td / "a"), ["-m", "pkg.a", "restart"])
    names = [p["name"] for p in lr.list_projects()]
    assert names == ["proj-b", "proj-a"], names   # дубликат proj-a не размножился

    d = lr.models_dir("chat")
    (d / "m.gguf").write_bytes(b"m")
    info = lr.switch_chat_model("m.gguf", restart=False)
    assert info["ok"] is True and info["file"] == "m.gguf"
    assert lr.read_current("chat")["file"] == "m.gguf"
    assert "applied" not in info                  # рестартов не было

    # неизвестный файл — отказ с понятным текстом
    bad = lr.switch_chat_model("ghost.gguf", restart=False)
    assert bad["ok"] is False and "ghost.gguf" in bad["msg"]

    # известный пресет, но скачивание запрещено
    preset = sorted(lr.CHAT_PRESETS)[0]
    info = lr.switch_chat_model(preset, restart=False, download=False)
    assert info["ok"] is False and info.get("need_download") is True
    print("TEST 4 OK: реестр проектов и switch_chat_model (без рестарта)")


def test_overview(td: Path):
    """chat_models_overview: файлы, пресеты, текущая, проекты."""
    d = lr.models_dir("chat")
    (d / "m.gguf").write_bytes(b"m")
    lr.set_current_chat("m.gguf")
    ov = lr.chat_models_overview()
    assert ov["current"] == "m.gguf"
    assert any(a["file"] == "m.gguf" for a in ov["available"])
    assert len(ov["presets"]) == len(lr.CHAT_PRESETS)
    assert all("downloaded" in p for p in ov["presets"])
    assert any(p["name"] == "proj-a" for p in ov["projects"])
    assert json.dumps(ov, ensure_ascii=False)     # сериализуется для UI
    print("TEST 5 OK: обзор рантайма для UI (файлы, пресеты, проекты)")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    with tempfile.TemporaryDirectory() as td:
        old = os.environ.get(lr.RUNTIME_DIR_ENV)
        os.environ[lr.RUNTIME_DIR_ENV] = td
        try:
            test_paths_follow_env()   # сам подменяет env, затем возвращает
            os.environ[lr.RUNTIME_DIR_ENV] = td
            test_manifest_and_resolve(Path(td))
            test_single_model_without_manifest(Path(td))
            test_registry_and_switch(Path(td))
            test_overview(Path(td))
        finally:
            if old is None:
                os.environ.pop(lr.RUNTIME_DIR_ENV, None)
            else:
                os.environ[lr.RUNTIME_DIR_ENV] = old
    print("\nALL LLAMA RUNTIME TESTS PASSED")


if __name__ == "__main__":
    main()

