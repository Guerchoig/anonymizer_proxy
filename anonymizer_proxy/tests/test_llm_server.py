"""
Тесты менеджера llama-server (llm_server).

Проверяют:
1. build_command: явные флаги (--port 8080 --parallel 1 --ctx-size 32768
   --no-webui), вычисление общего контекста (PARALLEL × CTX_PER_SLOT),
   проброс extra_args и ключа;
2. probe: трёхсторонняя детекция порта — живой llama / посторонний сервис /
   никто не слушает (HTTP подменяется мини-сервером, сеть не используется);
3. start: reuse живого инстанса без второго запуска; ошибка при чужом
   сервисе на порту;
4. stop по PID-файлу; битый PID-файл; отсутствие PID-файла.

Запуск: python -m anonymizer_proxy.tests.test_llm_server (из корня проекта)
"""
import json
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import anonymizer_proxy.llm_server as m

_TMP = None
_GGUF = None


def _make_cfg(**overrides) -> dict:
    """Тестовая конфигурация llama-server (временный GGUF-файл)."""
    cfg = dict(m.LLM_SERVER)
    cfg["bin"] = "llama-server-fake"
    cfg["model"] = str(_GGUF)
    cfg.update(overrides)
    return cfg


def _http_serve(responses: dict):
    """Мини-HTTP-сервер на эфемерном порту; responses: путь → (код, json).

    Возвращает (server, cfg) — cfg с реальным host:port для probe/start.
    """
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            code, body = responses.get(self.path, (404, {}))
            data = json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):  # тишина в консоли
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, {"host": "127.0.0.1", "port": port}


def test_build_command_defaults():
    """Явные флаги: порт, слоты, контекст, отключение WebUI."""
    cmd = m.build_command(_make_cfg())
    assert "8080" in cmd[cmd.index("--port") + 1]
    assert cmd[cmd.index("--parallel") + 1] == "1"
    assert cmd[cmd.index("--ctx-size") + 1] == "32768"
    assert "--no-webui" in cmd
    assert cmd[cmd.index("--host") + 1] == "127.0.0.1"
    assert "--cache-reuse" in cmd
    print("TEST 1 OK: build_command с дефолтами (порт 8080, 1 слот, 32K)")


def test_build_command_ctx_formula():
    """Общий контекст = PARALLEL × CTX_PER_SLOT."""
    cmd = m.build_command(_make_cfg(parallel=2, ctx_per_slot=32768))
    assert cmd[cmd.index("--ctx-size") + 1] == "65536"
    print("TEST 2 OK: ctx-size = parallel × ctx_per_slot (2 × 32768 = 65536)")


def test_build_command_extra_and_key():
    """extra_args в конце; api_key → --api-key; --webui отменяет --no-webui."""
    cmd = m.build_command(_make_cfg(
        api_key="secret", extra_args="--n-gpu-layers 99 --webui"))
    assert "--n-gpu-layers" in cmd and "99" in cmd
    assert cmd[cmd.index("--api-key") + 1] == "secret"
    assert "--no-webui" not in cmd and "--webui" in cmd
    print("TEST 3 OK: extra_args в конце, --webui переопределяет --no-webui")


def test_probe_llama():
    """Живой llama-server: /health 200 + /props с total_slots."""
    responses = {
        "/health": (200, {"status": "ok"}),
        "/props": (200, {"total_slots": 1, "model_path": "/m.gguf"}),
    }
    srv, cfg = _http_serve(responses)
    try:
        det = m.probe(cfg)
        assert det["state"] == m.STATE_LLAMA, det
        assert det["total_slots"] == 1
        assert det["props"]["model_path"] == "/m.gguf"
    finally:
        srv.shutdown()
    print("TEST 4 OK: llama-инстанс распознан по /props (total_slots)")


def test_probe_foreign():
    """Посторонний HTTP-сервис: /health 200, но /props не llama-формат."""
    responses = {"/health": (200, {"ok": True}), "/props": (200, {"foo": 1})}
    srv, cfg = _http_serve(responses)
    try:
        assert m.probe(cfg)["state"] == m.STATE_FOREIGN
    finally:
        srv.shutdown()
    print("TEST 5 OK: посторонний сервис на порту → state=foreign")


def test_probe_down():
    """Никто не слушает → state=down без исключений."""
    det = m.probe({"host": "127.0.0.1", "port": 1}, timeout=0.5)
    assert det["state"] == m.STATE_DOWN
    print("TEST 6 OK: порт свободен → state=down")


def test_start_reuses_alive():
    """Живой llama-инстанс переиспользуется: второй процесс не запускается."""
    responses = {
        "/health": (200, {"status": "ok"}),
        "/props": (200, {"total_slots": 1, "model_path": "/m.gguf"}),
    }
    srv, port_cfg = _http_serve(responses)
    try:
        info = m.start(_make_cfg(**port_cfg))
        assert info["reused"] is True and info["started"] is False, info
        assert info["total_slots"] == 1
    finally:
        srv.shutdown()
    print("TEST 7 OK: живой инстанс переиспользуется (reused=True)")


def test_start_rejects_foreign():
    """Чужой сервис на порту → RuntimeError с подсказкой, не молча."""
    responses = {"/health": (200, {"ok": True}), "/props": (200, {"foo": 1})}
    srv, port_cfg = _http_serve(responses)
    try:
        try:
            m.start(_make_cfg(**port_cfg))
            raise AssertionError("ожидали RuntimeError")
        except RuntimeError as exc:
            assert "посторонним сервисом" in str(exc)
    finally:
        srv.shutdown()
    print("TEST 8 OK: посторонний сервис на порту → отказ с подсказкой")


def test_start_warns_on_fewer_slots():
    """Живой инстанс с меньшим числом слотов → reuse (warning в лог)."""
    responses = {
        "/health": (200, {"status": "ok"}),
        "/props": (200, {"total_slots": 1}),
    }
    srv, port_cfg = _http_serve(responses)
    try:
        info = m.start(_make_cfg(parallel=2, **port_cfg))
        assert info["reused"] is True
    finally:
        srv.shutdown()
    print("TEST 8b OK: reuse 1-слотного чужого инстанса при PARALLEL=2")


def test_stop_pid_file():
    """stop: нет PID-файла / битый PID — безопасный False."""
    with tempfile.TemporaryDirectory() as td:
        m.PID_FILE = Path(td) / "llama_server.pid"
        assert m.stop() is False           # файла нет
        m.PID_FILE.write_text("not-a-pid", encoding="utf-8")
        assert m.stop() is False           # битый pid
        assert not m.PID_FILE.exists()     # файл вычищен
    print("TEST 9 OK: stop без/с битым PID-файлом безопасен")


def test_status_shape():
    """status() отдаёт поля для /api/backend и self-test."""
    responses = {
        "/health": (200, {"status": "ok"}),
        "/props": (200, {"total_slots": 1, "model_path": "/x.gguf"}),
    }
    srv, port_cfg = _http_serve(responses)
    try:
        st = m.status(_make_cfg(**port_cfg))
        assert st["running"] is True and st["total_slots"] == 1
        assert st["state"] == "llama" and "base_url" in st
        assert st["ctx_per_slot"] == 32768
    finally:
        srv.shutdown()
    print("TEST 10 OK: status() содержит слоты/ctx/модель")


def test_cli_check_exit_codes():
    """CLI check: exit 0 — живой llama, exit 1 — порт закрыт.

    Проверка «порт закрыт» идёт на ЭФЕМЕРНОМ порту мини-сервера (после его
    остановки), а не на боевом LLM_SERVER_PORT: на машине с общим
    llama-рантаймом на 8080 штатно живёт llama-server, и тест не должен от
    него зависеть.
    """
    responses = {
        "/health": (200, {"status": "ok"}),
        "/props": (200, {"total_slots": 1}),
    }
    srv, port_cfg = _http_serve(responses)
    saved = m.LLM_SERVER
    try:
        m.LLM_SERVER = _make_cfg(**port_cfg)
        assert m.main(["check"]) == 0
        srv.shutdown()                      # порт освобождён → state=down
        assert m.main(["check"]) == 1
    finally:
        srv.shutdown()
        m.LLM_SERVER = saved
    print("TEST 11 OK: CLI check — exit 0 живой / 1 down")


def main():
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    global _GGUF
    tmp = tempfile.TemporaryDirectory()
    _GGUF = Path(tmp.name) / "fake.gguf"
    _GGUF.write_bytes(b"x")
    try:
        test_build_command_defaults()
        test_build_command_ctx_formula()
        test_build_command_extra_and_key()
        test_probe_llama()
        test_probe_foreign()
        test_probe_down()
        test_start_reuses_alive()
        test_start_rejects_foreign()
        test_start_warns_on_fewer_slots()
        test_stop_pid_file()
        test_status_shape()
        test_cli_check_exit_codes()
    finally:
        tmp.cleanup()
    print("\nALL LLM SERVER TESTS PASSED")


if __name__ == "__main__":
    main()