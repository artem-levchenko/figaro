"""The shared secret of the HTTP API: which endpoints want it, and when the file is written."""
import asyncio
import os
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

import bridge
import cli_extras
import figaro
import figaro_token

ROOT = Path(__file__).resolve().parent.parent
GOOD = {"X-Figaro-Token": "test-token"}
CHANGING = [("/exec", {"code": "return 1"}), ("/clear", {}), ("/undo", {}), ("/reload", {}),
            ("/done", {}), ("/forget", {})]


def run(coro):
    return asyncio.run(coro)


async def client(token="test-token"):
    # conftest gives every client the right token; tests of the check set their own
    c = TestClient(TestServer(bridge.build_app()))
    if token is None:
        del c.session.headers["X-Figaro-Token"]
    elif token != "test-token":
        c.session.headers["X-Figaro-Token"] = token
    await c.start_server()
    return c


def test_changing_endpoints_refuse_a_missing_or_wrong_token():
    async def go():
        for token in (None, "wrong", ""):
            c = await client(token)
            for path, body in CHANGING:
                r = await c.post(path, json=body)
                assert r.status == 401, (path, token)
                j = await r.json()
                assert "X-Figaro-Token" in j["error"] and "FIGARO_TOKEN_FILE" in j["hint"]
                assert str(figaro_token.token_path(bridge.PORT)) in j["hint"]
            await c.close()
    run(go())


def test_the_right_token_gets_through():
    async def go():
        c = await client()
        for path, body in CHANGING:
            r = await c.post(path, json=body)
            assert r.status != 401, path  # 503 and the like: no plugin, but the guard let it by
        await c.close()
    run(go())


def test_a_bridge_without_a_token_refuses_everything(monkeypatch):
    monkeypatch.setattr(bridge, "TOKEN", None)

    async def go():
        c = await client("")
        assert (await c.post("/exec", json={"code": "1"})).status == 401
        await c.close()
    run(go())


def test_status_targets_and_root_stay_open():
    async def go():
        c = await client(None)
        for path in ("/status", "/targets", "/"):
            assert (await c.get(path)).status == 200, path
        await c.close()
    run(go())


def test_the_plugin_socket_does_not_want_it():
    async def go():
        c = await client(None)
        ws = await c.ws_connect("/plugin", headers={"Origin": "null"})
        await ws.close()
        await c.close()
    run(go())


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start(port, token_file):
    env = dict(os.environ, FIGARO_TOKEN_FILE=str(token_file), FIGARO_NO_UPDATE_CHECK="1")
    return subprocess.Popen([sys.executable, str(ROOT / "bridge.py"), "--port", str(port)], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_up(port):
    for _ in range(100):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=2).read()
            return
        except OSError:
            time.sleep(0.1)
    raise AssertionError("the bridge did not come up")


def post(port, token):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/clear", data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json",
                                          **({"X-Figaro-Token": token} if token else {})})
    try:
        return urllib.request.urlopen(req, timeout=5).status
    except urllib.error.HTTPError as e:
        return e.code


def test_the_file_is_written_after_bind_and_a_second_bridge_leaves_it_alone(tmp_path, monkeypatch):
    port = free_port()
    f = tmp_path / "sub" / "token"
    monkeypatch.setenv("FIGARO_TOKEN_FILE", str(f))
    first = start(port, f)
    second = None
    try:
        wait_up(port)
        token = f.read_text(encoding="utf-8")
        assert len(token) >= 40
        assert post(port, token) != 401 and post(port, "nope") == 401
        if sys.platform != "win32":
            assert stat.S_IMODE(f.stat().st_mode) == 0o600
        second = start(port, f)  # the port is busy: it dies before it writes
        assert second.wait(30) != 0
        assert f.read_text(encoding="utf-8") == token
        assert post(port, token) != 401  # the first one still takes it
        # the CLI reads the same file
        assert figaro_token.read_token(port) == token
    finally:
        for p in (first, second):
            if p and p.poll() is None:
                p.kill()
            if p:
                p.wait(10)


def test_a_restart_overwrites_the_file_and_fixes_a_loose_mode(tmp_path):
    port = free_port()
    f = tmp_path / "token"
    f.write_text("old")
    if sys.platform != "win32":
        f.chmod(0o644)
    p = start(port, f)
    try:
        wait_up(port)
        assert f.read_text(encoding="utf-8") != "old"
        if sys.platform != "win32":
            assert stat.S_IMODE(f.stat().st_mode) == 0o600
    finally:
        p.kill()
        p.wait(10)


def test_a_bridge_that_cannot_save_the_token_says_where_and_exits(tmp_path):
    blocker = tmp_path / "a-file"
    blocker.write_text("")
    f = blocker / "token"  # its folder is a file: neither the folder nor the token can be made
    env = dict(os.environ, FIGARO_TOKEN_FILE=str(f), FIGARO_NO_UPDATE_CHECK="1")
    p = subprocess.run([sys.executable, str(ROOT / "bridge.py"), "--port", str(free_port())],
                       env=env, capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert p.returncode == 1
    assert str(f) in p.stderr and "FIGARO_TOKEN_FILE" in p.stderr
    assert "Traceback" not in p.stderr


def test_two_ports_get_two_files_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("FIGARO_TOKEN_FILE")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert figaro_token.token_path(8788) != figaro_token.token_path(8789)
    assert figaro_token.token_path(8788) == tmp_path / ".figaro" / "token-8788"


def test_the_cli_sends_the_token_from_the_file(monkeypatch, tmp_path):
    f = tmp_path / "token"
    f.write_text("abc\n")
    monkeypatch.setenv("FIGARO_TOKEN_FILE", str(f))
    seen = []

    def fetch(host, port, method, path, body=None, headers=None, timeout=None, limit=None, on_logs=None):
        if path == "/":  # the look at the port, which carries nothing
            assert not headers
            return 200, b'{"service": "figaro-bridge"}'
        seen.append(headers)
        return 200, b"{}"

    monkeypatch.setattr(cli_extras, "fetch", fetch)
    figaro._request("POST", "/clear", {})
    assert seen[0]["X-Figaro-Token"] == "abc"
    f.unlink()
    figaro._request("POST", "/clear", {})
    assert "X-Figaro-Token" not in seen[1]


def test_doctor_says_when_the_token_file_is_missing(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("FIGARO_TOKEN_FILE", str(tmp_path / "nope"))
    monkeypatch.setattr(figaro, "_request", lambda *a, **k: (200, {"version": "1"}))
    assert figaro.cmd_doctor(None) == 1
    out = capsys.readouterr().out
    assert "no token in" in out and "nope" in out


def test_doctor_says_when_the_bridge_refuses_the_token(monkeypatch, capsys, tmp_path):
    f = tmp_path / "token"
    f.write_text("stale")
    monkeypatch.setenv("FIGARO_TOKEN_FILE", str(f))

    def fake(method, path, payload=None, timeout=65):
        if path == "/status":
            return 200, {"version": "1", "plugin_connected": True, "files": []}
        return 401, {"ok": False, "error": "missing or wrong X-Figaro-Token"}
    monkeypatch.setattr(figaro, "_request", fake)
    assert figaro.cmd_doctor(None) == 1
    assert "refused the token" in capsys.readouterr().out
