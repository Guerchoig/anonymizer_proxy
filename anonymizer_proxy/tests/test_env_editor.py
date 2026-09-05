"""
Тесты редактора .env (env_editor): белый список, хирургическая правка,
сохранение формата файла, бэкапы, маскировка секретов.

Проверяют:
1. Замена значения ключа сохраняет комментарии, порядок строк и формат
   файла (BOM, CRLF/LF определяются по исходнику).
2. Отсутствующий ключ дописывается в конец; null закомментирует строку,
   сохраняя прежнее значение.
3. Чужие ключи и невалидные значения отвергаются (файл не меняется).
4. read_schema маскирует секреты (value=None, только has_value) и
   возвращает значения обычных ключей.
5. Бэкап создаётся перед записью.

Запуск: python anonymizer_proxy\\tests\\test_env_editor.py (из корня)
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from anonymizer_proxy.env_editor import (
    EnvEditorError, apply_updates, read_schema, schema,
)

BASE = ("# Комментарий сверху\n"
        "\n"
        "PROXY_PORT=8081\n"
        "#OLD_KEY=old-value\n"
        "OPENROUTER_API_KEY=sk-or-v1-test1234567890\n"
        "OPENROUTER_MODEL=qwen/qwen-3.7-max\n")


def _write(tmp: Path, content: str, bom: bool = False) -> Path:
    p = tmp / ".env"
    p.write_bytes((b"\xef\xbb\xbf" if bom else b"") + content.encode("utf-8"))
    return p


def test_replace_keeps_comments_and_format():
    """Замена: комментарии/порядок целы, CRLF и BOM сохраняются"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _write(tmp, BASE.replace("\n", "\r\n"), bom=True)
        bak = tmp / "bak"
        r = apply_updates({"PROXY_PORT": "9090"}, env_path=env,
                          backup_dir=bak)
        assert r["changed"] == ["PROXY_PORT"]
        data = env.read_bytes()
        assert data.startswith(b"\xef\xbb\xbf"), "BOM потерян"
        assert b"\r\n" in data and b"\n" not in data.replace(b"\r\n", b""), \
            "CRLF нарушен"
        text = data.decode("utf-8-sig")
        lines = text.split("\r\n")
        assert lines[0] == "# Комментарий сверху"
        assert "PROXY_PORT=9090" in lines
        assert "#OLD_KEY=old-value" in lines
        assert "OPENROUTER_API_KEY=sk-or-v1-test1234567890" in lines
        assert r["backup"] and Path(r["backup"]).is_file()
    print("TEST 1 OK: замена ключа, комментарии и CRLF+BOM сохранены")


def test_lf_file_stays_lf():
    """Файл с LF (типичный для macOS) остаётся LF без BOM"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _write(tmp, BASE)  # LF, без BOM
        apply_updates({"OPENROUTER_MODEL": "qwen/qwen-3.7-plus"},
                      env_path=env, backup_dir=tmp / "bak")
        data = env.read_bytes()
        assert not data.startswith(b"\xef\xbb\xbf")
        assert b"\r" not in data, "LF файл испорчен CRLF"
        assert b"OPENROUTER_MODEL=qwen/qwen-3.7-plus" in data
    print("TEST 2 OK: LF-файл остаётся LF без BOM")


def test_append_missing_key():
    """Ключ из белого списка, отсутствующий в .env, дописывается"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _write(tmp, BASE)
        r = apply_updates({"GPTUNNEL_API_KEY": "gt-key-1"},
                          env_path=env, backup_dir=tmp / "bak")
        assert r["added"] == ["GPTUNNEL_API_KEY"]
        text = env.read_text(encoding="utf-8")
        assert text.rstrip().endswith("GPTUNNEL_API_KEY=gt-key-1")
    print("TEST 3 OK: отсутствующий ключ дописан в конец")


def test_null_comments_out():
    """null закомментирует строку, сохраняя прежнее значение"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _write(tmp, BASE)
        r = apply_updates({"OPENROUTER_API_KEY": None},
                          env_path=env, backup_dir=tmp / "bak")
        assert r["removed"] == ["OPENROUTER_API_KEY"]
        text = env.read_text(encoding="utf-8")
        assert "# OPENROUTER_API_KEY=sk-or-v1-test1234567890" in text
    print("TEST 4 OK: null закомментировал ключ со старым значением")


def test_rejects():
    """Чужой ключ, неверный int, неверный выбор, секрет с переводом строки"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _write(tmp, BASE)
        before = env.read_bytes()
        for updates, err_part in [
            ({"SOME_RANDOM_KEY": "1"}, "белый список"),
            ({"PROXY_PORT": "99999"}, "число"),
            ({"CLOUD_PROVIDER": "yandex"}, "допустимые значения"),
            ({"GPTUNNEL_API_KEY": "a\nb"}, "переводы строк"),
            ({"GPTUNNEL_API_KEY": 'key"quote'}, "кавычки"),
        ]:
            try:
                apply_updates(updates, env_path=env, backup_dir=tmp / "bak")
                assert False, f"должен быть EnvEditorError для {updates}"
            except EnvEditorError as e:
                assert err_part in str(e), f"{updates}: {e}"
        assert env.read_bytes() == before, "файл изменился при ошибке"
    print("TEST 5 OK: чужие ключи и невалидные значения отвергаются")


def test_schema_masks_secrets():
    """read_schema: секреты — has_value + маска, полного значения нет"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _write(tmp, BASE)
        s = read_schema(env_path=env)
        items = {i["key"]: i for i in s["items"]}
        assert len(items) >= 15, "белый список подозрительно мал"
        sec = items["OPENROUTER_API_KEY"]
        assert sec["is_secret"] and sec["has_value"] and sec["value"] is None, sec
        # маска: первые 3 + последние 4 символа
        assert sec["masked"] == "sk-…7890", sec["masked"]
        assert "sk-or-v1" not in str(s), "секрет попал в схему!"
        model = items["OPENROUTER_MODEL"]
        assert model["value"] == "qwen/qwen-3.7-max" and not model["is_secret"]
        # placeholder-ключ из .env.example не считается «заданным»
        env2 = _write(tmp, BASE + "GPTUNNEL_API_KEY=REPLACE_WITH_YOUR_KEY\n")
        s2 = read_schema(env_path=env2)
        items2 = {i["key"]: i for i in s2["items"]}
        assert items2["GPTUNNEL_API_KEY"]["has_value"] is False
        assert items2["GPTUNNEL_API_KEY"]["masked"] is None
    print("TEST 6 OK: секреты маскируются (sk-…7890), обычные ключи читаются")


def test_inmemory_registry_sync():
    """Правки .env из формы синхронизируются в работающий реестр
    (без перезапуска): модель, ключ, очистка ключа через null"""
    from anonymizer_proxy.config import CLOUD_PROVIDERS
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _write(tmp, BASE)
        old_model = CLOUD_PROVIDERS["openrouter"]["model"]
        old_gptunnel_key = CLOUD_PROVIDERS["gptunnel"]["api_key"]
        try:
            apply_updates({"OPENROUTER_MODEL": "qwen/test-sync"},
                          env_path=env, backup_dir=tmp / "bak")
            assert CLOUD_PROVIDERS["openrouter"]["model"] == "qwen/test-sync", \
                "модель не синхронизирована в работающий реестр"

            apply_updates({"GPTUNNEL_API_KEY": "gt-key-2"},
                          env_path=env, backup_dir=tmp / "bak")
            assert CLOUD_PROVIDERS["gptunnel"]["api_key"] == "gt-key-2"

            apply_updates({"GPTUNNEL_API_KEY": None},
                          env_path=env, backup_dir=tmp / "bak")
            assert CLOUD_PROVIDERS["gptunnel"]["api_key"] == "", \
                "очистка ключа не синхронизирована"
        finally:
            CLOUD_PROVIDERS["openrouter"]["model"] = old_model
            CLOUD_PROVIDERS["gptunnel"]["api_key"] = old_gptunnel_key
    print("TEST 7 OK: правки .env синхронизируются в работающий реестр")


def test_chad_removed_from_whitelist():
    """Chad удалён: его ключей нет в белом списке формы"""
    keys = {m["key"] for m in schema()}
    assert "CHAD_API_KEY" not in keys
    assert "CHAD_MODEL" not in keys
    print("TEST 8 OK: Chad вычищен из белого списка редактора")


def test_noop_and_idempotency():
    """Повторная запись того же значения — без изменений; null для
    отсутствующего ключа — не ошибка"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _write(tmp, BASE)
        r = apply_updates({"OPENROUTER_MODEL": "qwen/qwen-3.7-max"},
                          env_path=env, backup_dir=tmp / "bak")
        assert r["changed"] == [] and r["added"] == []
        r2 = apply_updates({"GPTUNNEL_API_KEY": None},
                           env_path=env, backup_dir=tmp / "bak")
        assert r2["removed"] == []
    print("TEST 7 OK: идемпотентность (no-op без изменений)")


def test_whitelist_covers_providers():
    """В белом списке есть ключи всех провайдеров реестра"""
    from anonymizer_proxy.config import CLOUD_PROVIDERS
    keys = {m["key"] for m in schema()}
    for name in CLOUD_PROVIDERS:
        key_env = ("OPENROUTER_API_KEY" if name == "openrouter"
                   else f"{name.upper()}_API_KEY")
        assert key_env in keys, f"нет {key_env} в белом списке"
    print("TEST 8 OK: белый список покрывает всех провайдеров")


def main():
    test_replace_keeps_comments_and_format()
    test_lf_file_stays_lf()
    test_append_missing_key()
    test_null_comments_out()
    test_rejects()
    test_schema_masks_secrets()
    test_inmemory_registry_sync()
    test_chad_removed_from_whitelist()
    test_noop_and_idempotency()
    test_whitelist_covers_providers()
    print("\nALL ENV EDITOR TESTS PASSED")


if __name__ == "__main__":
    main()
