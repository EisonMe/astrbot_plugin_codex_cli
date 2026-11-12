"""Exercise desktop RPC and notification lifecycle over a real Unix WebSocket."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiohttp import web
from astrbot_plugin_codex_cli.app_server import AppServerClient
from astrbot_plugin_codex_cli.runner import CodexRunner

THREAD = "22222222-2222-4222-8222-222222222222"


@pytest_asyncio.fixture
async def desktop(tmp_path):
    """Provide a shared desktop server that stays alive across client connections.

    Args:
        tmp_path: Isolated socket and job directory.

    Yields:
        Runner and mutable server state.
    """
    state = SimpleNamespace(
        calls=[],
        starts=0,
        interrupts=0,
        active=False,
        sockets=[],
        mode="normal",
        name="",
    )
    workers = []

    async def notify(ws, method, **params):
        if not ws.closed:
            await ws.send_json(
                {"method": method, "params": {"threadId": THREAD, **params}}
            )

    async def finish(ws):
        await asyncio.sleep(0.03)
        if state.mode in {"hold", "lost_start"}:
            return
        if state.mode == "disconnect":
            await ws.close()
            return
        await notify(
            ws,
            "item/completed",
            turnId="turn-1",
            item={
                "id": "msg",
                "type": "agentMessage",
                "phase": "commentary",
                "text": "Still working",
            },
        )
        if state.mode != "failed":
            await notify(
                ws,
                "item/agentMessage/delta",
                turnId="turn-1",
                itemId="final",
                delta="live progress",
            )
            await notify(
                ws,
                "item/completed",
                turnId="turn-1",
                item={
                    "id": "final",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": "Done test-secret-123456789",
                },
            )
        # Noise from another task must not become this task's result or completion.
        await ws.send_json(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "other",
                    "turn": {"id": "other-turn", "status": "failed"},
                },
            }
        )
        state.active = False
        await notify(
            ws,
            "turn/completed",
            turn={
                "id": "turn-1",
                "status": "failed" if state.mode == "failed" else "completed",
                "error": {"message": "test failure"}
                if state.mode == "failed"
                else None,
            },
        )

    async def socket(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        state.sockets.append(ws)
        async for message in ws:
            data = json.loads(message.data)
            method, params = data["method"], data.get("params", {})
            state.calls.append((method, params))
            if "id" not in data:
                continue
            result = {}
            if method in {"thread/start", "thread/resume", "thread/read"}:
                result = {
                    "thread": {
                        "id": THREAD,
                        "status": {"type": "active" if state.active else "idle"},
                        "turns": [{"id": "turn-1", "status": "inProgress"}]
                        if state.active
                        else [],
                    }
                }
            elif method == "thread/name/set":
                state.name = params["name"]
            elif method == "turn/start":
                state.starts += 1
                state.active = True
                if state.mode != "lost_start":
                    await notify(
                        ws,
                        "turn/started",
                        turn={"id": "turn-1", "status": "inProgress"},
                    )
                workers.append(asyncio.create_task(finish(ws)))
                if state.mode == "lost_start":
                    continue
                result = {"turn": {"id": "turn-1", "status": "inProgress"}}
            elif method == "turn/interrupt":
                state.interrupts += 1
                state.active = False
                await notify(
                    ws, "turn/completed", turn={"id": "turn-1", "status": "interrupted"}
                )
            await ws.send_json({"id": data["id"], "result": result})
        return ws

    app = web.Application()
    app.router.add_get("/", socket)
    server = web.AppRunner(app)
    await server.setup()
    # Pytest's nested directory can exceed the Unix socket path limit.
    import tempfile

    with tempfile.TemporaryDirectory(prefix="codex-test-") as temporary:
        path = Path(temporary) / "app.sock"
        await web.UnixSite(server, str(path)).start()
        env = tmp_path / "env.json"
        env.write_text(json.dumps({"TEST_API_KEY": "test-secret-123456789"}))
        runner = CodexRunner(
            tmp_path / "data",
            {
                "codex_path": shutil.which("true"),
                "desktop_link": True,
                "app_server_socket": str(path),
                "default_workdir": str(tmp_path),
                "environment_file": str(env),
                "sandbox": "workspace-write",
                "model": "test-model",
            },
        )
        try:
            yield runner, state
        finally:
            await runner.close()
            for ws in state.sockets:
                await ws.close()
            await asyncio.gather(*workers, return_exceptions=True)
            await server.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("title_prefix", [None, "My Assistant"])
async def test_live_task_result_and_resume_use_same_desktop_thread(
    desktop, title_prefix
):
    """The same server receives title, task, permission overrides and resume."""
    runner, state = desktop
    if title_prefix is not None:
        runner.config["task_title_prefix"] = title_prefix
    prompt = "中文 task; $(touch injected) `pwd`"
    first = runner.start("owner", prompt)
    await runner.tasks[first["job_id"]]
    job = runner.get(first["job_id"], "owner")
    assert job["status"] == "completed"
    assert job["result"] == "Done [REDACTED]"
    assert job["session_id"] == THREAD
    assert runner.view(job)["desktop_link"] is True
    assert state.name.startswith(f"{title_prefix or 'AstrBot'} · ")
    params = next(params for method, params in state.calls if method == "thread/start")
    assert params["sandbox"] == "workspace-write"
    assert params["model"] == "test-model"
    assert params["approvalPolicy"] == "never"
    turn = next(params for method, params in state.calls if method == "turn/start")
    assert turn["input"] == [{"type": "text", "text": prompt}]
    assert not (Path(job["workdir"]) / "injected").exists()
    await asyncio.sleep(0)
    second = runner.start("owner", "Continue", resume_job_id=job["job_id"])
    await runner.tasks[second["job_id"]]
    assert second["session_id"] == THREAD
    assert state.starts == 2
    assert any(method == "thread/resume" for method, _ in state.calls)
    for path in (runner.jobs_dir / job["job_id"]).iterdir():
        assert "test-secret-123456789" not in path.read_text()
        assert path.stat().st_mode & 0o077 == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["hold", "lost_start"])
async def test_cancel_interrupts_only_owned_turn_even_if_start_response_lost(
    desktop, mode
):
    """Cancellation reaches the daemon instead of just disconnecting a client."""
    runner, state = desktop
    state.mode = mode
    job = runner.start("owner", "Work")
    for _ in range(100):
        if state.starts:
            break
        await asyncio.sleep(0.01)
    result = await runner.cancel(job["job_id"], "owner")
    if mode == "lost_start":
        assert result["status"] == "interrupted"
        assert state.interrupts == 0
        assert state.active
        assert "未能确认" in result["error"]
        return
    assert result["status"] == "cancelled"
    assert state.interrupts == 1
    assert not state.active
    assert not runner.processes
    interrupt = next(
        params for method, params in state.calls if method == "turn/interrupt"
    )
    assert interrupt == {"threadId": THREAD, "turnId": "turn-1"}


@pytest.mark.asyncio
async def test_timeout_interrupts_desktop_turn(desktop):
    """The shared server survives a timed out task."""
    runner, state = desktop
    state.mode = "hold"
    runner.config["timeout_seconds"] = 5
    job = runner.start("owner", "Work")
    await runner.tasks[job["job_id"]]
    assert job["status"] == "timed_out"
    assert state.interrupts == 1
    assert not state.active


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["failed", "disconnect"])
async def test_failures_never_retry_or_report_commentary_as_success(desktop, mode):
    """Lost connections do not duplicate work; commentary is not a final answer."""
    runner, state = desktop
    state.mode = mode
    job = runner.start("owner", "Work")
    await runner.tasks[job["job_id"]]
    assert job["status"] == "failed"
    assert job["result"] == ""
    assert state.starts == 1
    assert not state.active
    if mode == "disconnect":
        assert state.interrupts == 1


@pytest.mark.asyncio
async def test_resuming_active_desktop_thread_does_not_steer_it(desktop):
    """A task still running on the desktop is checked before any mutation."""
    runner, state = desktop
    first = runner.start("owner", "First")
    await runner.tasks[first["job_id"]]
    await asyncio.sleep(0)
    state.active = True
    before = len(state.calls)
    second = runner.start("owner", "Continue", resume_job_id=first["job_id"])
    await runner.tasks[second["job_id"]]
    assert second["status"] == "failed"
    assert state.starts == 1
    assert state.interrupts == 0
    assert not any(
        method in {"thread/resume", "thread/name/set"}
        for method, _ in state.calls[before:]
    )


@pytest.mark.asyncio
async def test_missing_socket_fails_without_launching_invisible_cli(desktop):
    """App mode fails explicitly when no connection can be established."""
    runner, state = desktop
    runner.config["app_server_socket"] = "/tmp/nonexistent-astrbot-codex-socket"
    job = runner.start("owner", "Work")
    await runner.tasks[job["job_id"]]
    assert job["status"] == "failed"
    assert state.starts == 0
    assert not runner.processes


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["system", "service", "ambiguous"])
async def test_socket_discovery_with_service_tmpdir(tmp_path, monkeypatch, location):
    """Find the desktop outside a service TMPDIR without guessing among sockets."""
    import os
    import socket

    from astrbot_plugin_codex_cli import app_server

    service_tmp = tmp_path / "service"
    system_socket = tmp_path / "desktop.sock"
    service_socket = service_tmp / f"codex-daemon-{os.getuid()}" / "app.sock"
    sockets = []
    for path in (
        [system_socket, service_socket]
        if location == "ambiguous"
        else [system_socket if location == "system" else service_socket]
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        sock = socket.socket(socket.AF_UNIX)
        sock.bind(str(path))
        sockets.append(sock)
    monkeypatch.setattr(app_server.tempfile, "gettempdir", lambda: str(service_tmp))
    original_glob = Path.glob

    def isolated_glob(path, pattern):
        if path == Path("/tmp") / f"codex-daemon-{os.getuid()}":
            return iter([system_socket]) if system_socket.exists() else iter([])
        return original_glob(path, pattern)

    monkeypatch.setattr(Path, "glob", isolated_glob)
    connected = []

    class DiscoveryFinished(Exception):
        pass

    def connector(*, path):
        connected.append(path)
        raise DiscoveryFinished

    monkeypatch.setattr(app_server.aiohttp, "UnixConnector", connector)
    client = AppServerClient("", lambda _: None)
    try:
        if location == "ambiguous":
            with pytest.raises(ValueError, match="唯一"):
                await client.connect()
            assert not connected
        else:
            with pytest.raises(DiscoveryFinished):
                await client.connect()
            assert connected == [
                str(system_socket if location == "system" else service_socket)
            ]
    finally:
        for sock in sockets:
            sock.close()
