"""
Тесты мигратора .env (scripts/ensure_env_keys.py) — шага установщика, без
которого обновление поверх старой установки ломает локальную модель.

Проверяют: добавление отсутствующих ключей по .env.example, дедупликацию
(python-dotenv молча берёт ПОСЛЕДНЕЕ значение — из-за этого строка, добавленная
выше, не действует), комментирование устаревшего ключа, снятие конфликтующего
LOCAL_LLM_BASE_URL, идемпотентность и режим --check (без записи).

Запуск: python anonymizer_proxy\\tests\\test_ensure_env_keys.py (из корня проекта)
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location(
    "ensure_env_keys", ROOT / "scripts" / "ensure_env_keys.py")
assert _spec and _spec.loader, "не найден scripts/ensure_env_keys.py"
env_keys_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(env_keys_mod)

EXAMPLE = """\
OPENROUTER_API_KEY=sk-or-v1-REPLACE_WITH_YOUR_KEY
LLM_SERVER_MODEL=shared:chat
LLM_SERVER_HOST=127.0.0.1
LLM_SERVER_PORT=8080
LOCAL_LLM_TIMEOUT=600
PROXY_PORT=8081
"""


def _write(td: Path, name: str, text: str) -> Path:
    path = td / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _run(td: Path, env_text: str, check: bool = False):
    example = _write(td, ".env.example", EXAMPLE)
    env = _write(td, ".env", env_text)
    added, warnings = env_keys_mod.ensure_env(example, env, check=check)
    return added, warnings, env.read_text(encoding="utf-8")


def test_missing_keys_are_appended():
    """Отсутствующие ключи дописываются со значениями .env.example."""
    import tempfile
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        added, _, text = _run(td, "OPENROUTER_API_KEY=x\nPROXY_PORT=8081\n")
        assert "LLM_SERVER_MODEL" in added and "LLM_SERVER_PORT" in added, added
        assert "LLM_SERVER_MODEL=shared:chat" in text, text
        assert "OPENROUTER_API_KEY=x" in text          # значение не перезаписано
    print("TEST 1 OK: отсутствующие ключи дописываются из .env.example")


def test_duplicate_key_keeps_last_and_warns():
    """Дубль ключа: активным остаётся ПОСЛЕДНЕЕ значение (как читает dotenv)."""
    import tempfile
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        _, warnings, text = _run(
            td, "LLM_SERVER_PORT=8010\nLLM_SERVER_MODEL=shared:chat\n"
                "LLM_SERVER_PORT=8080\n")
        active = [ln for ln in text.splitlines()
                  if ln.startswith("LLM_SERVER_PORT=")]
        assert active == ["LLM_SERVER_PORT=8080"], active
        assert any("дубль ключа LLM_SERVER_PORT" in w for w in warnings), warnings
    print("TEST 2 OK: дубль ключа -> комментарий + предупреждение")


def test_obsolete_and_conflicting_commented():
    """Устаревший ключ и конфликтующий LOCAL_LLM_BASE_URL уводятся в комментарий."""
    import tempfile
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        _, warnings, text = _run(
            td, "LLM_SERVER_PORT=8080\n"
                "LOCAL_LLM_BASE_URL=http://127.0.0.1:8010/v1\n"
                "LOCAL_LLM_MIN_MAX_TOKENS=16384\n")
        assert "# LOCAL_LLM_MIN_MAX_TOKENS=16384" in text, text
        assert "# LOCAL_LLM_BASE_URL=http://127.0.0.1:8010/v1" in text, text
        assert any("LOCAL_LLM_BASE_URL" in w for w in warnings), warnings
        # совпадающий порт конфликтом не считается
        _, w2, text2 = _run(
            Path(t) / "ok_env", "LLM_SERVER_PORT=8080\n"
                                "LOCAL_LLM_BASE_URL=http://127.0.0.1:8080/v1\n")
        assert "LOCAL_LLM_BASE_URL=http://127.0.0.1:8080/v1" in text2
        assert not any("LOCAL_LLM_BASE_URL" in w for w in w2), w2
    print("TEST 3 OK: устаревшие/конфликтующие ключи -> комментарий")


def test_idempotent_and_check_mode():
    """Повторный прогон ничего не меняет; --check не пишет на диск."""
    import tempfile
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        _added, _, text1 = _run(td, "OPENROUTER_API_KEY=x\n")
        example = td / ".env.example"
        env = td / ".env"
        added2, _ = env_keys_mod.ensure_env(example, env)
        text2 = env.read_text(encoding="utf-8")
        assert added2 == [] and text2 == text1, (added2, text2)

        short = _write(td, "short.env", "OPENROUTER_API_KEY=x\n")
        before = short.read_text(encoding="utf-8")
        added3, _ = env_keys_mod.ensure_env(example, short, check=True)
        assert added3, "check должен сообщить об отсутствующих ключах"
        assert short.read_text(encoding="utf-8") == before, "check не должен писать"
    print("TEST 4 OK: идемпотентность и режим --check (без записи)")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    test_missing_keys_are_appended()
    test_duplicate_key_keeps_last_and_warns()
    test_obsolete_and_conflicting_commented()
    test_idempotent_and_check_mode()
    print("\nALL ENSURE_ENV_KEYS TESTS PASSED")


if __name__ == "__main__":
    main()
