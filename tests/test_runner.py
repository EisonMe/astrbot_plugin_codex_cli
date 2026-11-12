"""Exercise real subprocess IO, cancellation, authorization and resume isolation."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import signal
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_codex_cli.main import CodexCliPlugin
from astrbot_plugin_codex_cli.notifications import CompletionNotifications
from astrbot_plugin_codex_cli.runner import CodexRunner

FAKE_CLI = r"""
import json, os, signal, subprocess, sys, time
from pathlib import Path
args = sys.argv[1:]
prompt = sys.stdin.read()
print(json.dumps({"type": "thread.started", "thread_id": "11111111-1111-4111-8111-111111111111"}), flush=True)
if prompt == "sleep":
    child = subprocess.Popen([sys.executable, "-c", "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(120)"])
    Path("child.pid").write_text(str(child.pid))
    time.sleep(120)
elif prompt == "fail":
    print(json.dumps({"type":"turn.failed", "error":{"message":"fake failure"}}), flush=True)
    print(json.dumps({"type":"item.completed", "item":{"type":"agent_message", "text":"partial work"}}), flush=True)
    sys.exit(0)
elif prompt == "no-output":
    sys.exit(0)
sys.stderr.write("x" * 150000 + "\n")
text = json.dumps({"prompt":prompt,"argv":args,"secret":os.environ.get("TEST_API_KEY","")})
Path(args[args.index("-o")+1]).write_text(text)
print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":text}}), flush=True)
print(json.dumps({"type":"turn.completed","usage":{"input_tokens":7,"output_tokens":3}}), flush=True)
"""


@pytest.fixture
def runner(tmp_path):
    """Provide a CLI executable that exercises pipes without calling a model.

    Args:
        tmp_path: Isolated pytest directory.

    Yields:
        Configured runner for isolated subprocess tests.
    """
    executable = tmp_path / "fake-codex"
    executable.write_text(f"#!{sys.executable}\n" + FAKE_CLI)
    executable.chmod(0o700)
    env_file = tmp_path / "env.json"
    env_file.write_text(json.dumps({"TEST_API_KEY": "test-secret-value-987654"}))
    env_file.chmod(0o600)
    return CodexRunner(
        tmp_path / "data",
        {
            "codex_path": str(executable),
            "default_workdir": str(tmp_path),
            "environment_file": str(env_file),
            "sandbox": "workspace-write",
            "desktop_link": False,
        },
    )


async def finished(runner, job):
    """Await a specific worker and reload its durable status.

    Args:
        runner: Runner under test.
        job: Initial metadata containing the task identifier.

    Returns:
        Persisted final job metadata.
    """
    await runner.tasks[job["job_id"]]
    await asyncio.sleep(0)
    return runner.get(job["job_id"], "owner")


@pytest.mark.asyncio
async def test_stdin_no_shell_and_large_stderr_redaction(runner, tmp_path):
    """Task metacharacters stay on stdin and secrets never enter saved output."""
    runner.config.pop("desktop_link")
    prompt = f"中文 with spaces; $(touch {tmp_path / 'injected'}) `pwd`"
    job = await finished(runner, runner.start("owner", prompt))
    assert job["status"] == "completed"
    assert job["desktop_link"] is False
    result = json.loads(job["result"])
    assert result["prompt"] == prompt
    assert result["secret"] == "[REDACTED]"
    assert prompt not in result["argv"]
    assert 'sandbox_mode="workspace-write"' in result["argv"]
    assert not (tmp_path / "injected").exists()
    assert job["usage"]["input_tokens"] == 7
    for path in (runner.jobs_dir / job["job_id"]).iterdir():
        assert "test-secret-value-987654" not in path.read_text()
        assert path.stat().st_mode & 0o077 == 0


@pytest.mark.asyncio
async def test_resume_is_bound_to_original_owner(runner):
    """Only an owning session can resume a known thread by plugin job ID."""
    first = await finished(runner, runner.start("owner", "first"))
    with pytest.raises(ValueError):
        runner.start("stranger", "resume", resume_job_id=first["job_id"])
    second = await finished(
        runner, runner.start("owner", "second", resume_job_id=first["job_id"])
    )
    args = json.loads(second["result"])["argv"]
    assert "resume" in args
    assert first["session_id"] in args
    assert second["resume_job_id"] == first["job_id"]


@pytest.mark.asyncio
async def test_status_page_and_reload_recovery(runner):
    """Pages cover output and a fresh runner reports abandoned jobs truthfully."""
    job = await finished(runner, runner.start("owner", "z" * 10000))
    page = runner.view(job, 0, 500)
    assert len(page["result"]) == page["next_offset"] == 500
    assert "owner" not in page
    assert runner.get("", "owner")["job_id"] == job["job_id"]
    with pytest.raises(ValueError):
        runner.get("../../etc/passwd", "owner")
    job["status"] = "running"
    runner._save(job)
    restored = CodexRunner(runner.jobs_dir.parent, runner.config)
    assert restored.get(job["job_id"], "owner")["status"] == "interrupted"


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt", ["fail", "no-output"])
async def test_partial_or_empty_output_is_not_success(runner, prompt):
    """Zero exit status cannot hide a failed turn or an empty answer."""
    job = await finished(runner, runner.start("owner", prompt))
    assert job["status"] == "failed"


@pytest.mark.asyncio
async def test_cancellation_and_concurrency_stop_process_group(runner, tmp_path):
    """Cancellation kills descendants, including those ignoring SIGTERM."""
    job = runner.start("owner", "sleep")
    with pytest.raises(ValueError):
        runner.start("owner", "duplicate")
    for _ in range(100):
        if (tmp_path / "child.pid").exists():
            break
        await asyncio.sleep(0.02)
    child = int((tmp_path / "child.pid").read_text())
    assert (await runner.cancel(job["job_id"], "owner"))["status"] == "cancelled"
    for _ in range(100):
        stat = Path(f"/proc/{child}/stat")
        if not stat.exists() or stat.read_text().split()[2] == "Z":
            break
        await asyncio.sleep(0.02)
    else:
        os.kill(child, signal.SIGKILL)
        pytest.fail("CLI child process survived cancellation")
    assert not runner.processes


@pytest.mark.asyncio
async def test_cancel_before_worker_starts(runner):
    """Immediate cancellation still writes terminal metadata."""
    job = runner.start("owner", "sleep")
    assert (await runner.cancel(job["job_id"], "owner"))["status"] == "cancelled"
    assert not runner.tasks


@pytest.mark.asyncio
async def test_timeout_is_terminal(runner):
    """A hung CLI becomes timed_out instead of silently stopping the chat."""
    runner.config["timeout_seconds"] = 5
    job = await finished(runner, runner.start("owner", "sleep"))
    assert job["status"] == "timed_out"
    assert not runner.processes


@pytest.mark.asyncio
async def test_cancelling_tool_wait_leaves_background_job_running(runner, tmp_path):
    """Cancelling an AstrBot tool request must not cancel the owned CLI worker."""
    plugin = object.__new__(CodexCliPlugin)
    plugin.config = {"initial_wait_seconds": 20}
    plugin.runner = runner
    plugin.notifications = CompletionNotifications(runner, AsyncMock(return_value=True))
    event = SimpleNamespace(
        is_private_chat=lambda: True,
        is_admin=lambda: True,
        get_platform_id=lambda: "qq",
        get_sender_id=lambda: "123",
        unified_msg_origin="qq:FriendMessage:123",
    )
    request = asyncio.create_task(plugin.start(event, "sleep"))
    for _ in range(100):
        if (tmp_path / "child.pid").exists():
            break
        await asyncio.sleep(0.02)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    job = runner.get("", plugin.owner(event))
    assert job["status"] == "running"
    assert not runner.tasks[job["job_id"]].done()
    await runner.cancel(job["job_id"], plugin.owner(event))
    await plugin.notifications.deliver_pending()
    plugin.notifications.send_message.assert_awaited_once()
    assert (
        plugin.notifications.send_message.call_args.args[0] == event.unified_msg_origin
    )


@pytest.mark.asyncio
async def test_background_result_reports_to_original_chat_once(runner):
    """A returned background tool call still sends its final result without a new turn."""
    plugin = object.__new__(CodexCliPlugin)
    plugin.config = {"initial_wait_seconds": 0}
    plugin.runner = runner
    sender = AsyncMock(return_value=True)
    plugin.notifications = CompletionNotifications(runner, sender)
    event = SimpleNamespace(
        is_private_chat=lambda: True,
        is_admin=lambda: True,
        get_platform_id=lambda: "qq",
        get_sender_id=lambda: "123",
        unified_msg_origin="qq:FriendMessage:123",
    )
    initial = json.loads(await plugin.start(event, "write a result"))
    await runner.tasks[initial["job_id"]]
    await asyncio.sleep(0)
    await plugin.notifications.deliver_pending()
    sender.assert_awaited_once()
    origin, chain = sender.call_args.args
    assert origin == event.unified_msg_origin
    assert "已完成" in chain.get_plain_text()
    assert "write a result" in chain.get_plain_text()
    assert "test-secret-value-987654" not in chain.get_plain_text()
    await plugin.notifications.deliver_pending()
    # A restarted sender must respect the success saved by its predecessor.
    restored = CompletionNotifications(runner, sender)
    await restored.deliver_pending()
    sender.assert_awaited_once()


@pytest.mark.asyncio
async def test_inline_result_does_not_duplicate_the_initial_response(runner):
    """A terminal result already returned inline does not create an extra push."""
    plugin = object.__new__(CodexCliPlugin)
    plugin.config = {"initial_wait_seconds": 20}
    plugin.runner = runner
    plugin.notifications = CompletionNotifications(runner, AsyncMock(return_value=True))
    event = SimpleNamespace(
        is_private_chat=lambda: True,
        is_admin=lambda: True,
        get_platform_id=lambda: "qq",
        get_sender_id=lambda: "123",
        unified_msg_origin="qq:FriendMessage:123",
    )
    result = json.loads(await plugin.start(event, "quick result"))
    assert result["status"] == "completed"
    await plugin.notifications.deliver_pending()
    plugin.notifications.send_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("first_result", [False, RuntimeError("network offline")])
async def test_notification_retry_survives_reload(runner, first_result):
    """An unavailable adapter and a raised send error both remain retryable."""
    sender = AsyncMock(side_effect=[first_result, True])
    reports = CompletionNotifications(runner, sender)
    job = runner.start("owner", "final result")
    reports.register(job, "qq:FriendMessage:123")
    reports.release(job["job_id"])
    await runner.tasks[job["job_id"]]
    await asyncio.sleep(0)
    await reports.deliver_pending()
    path = runner.jobs_dir / job["job_id"] / "notification.json"
    notice = json.loads(path.read_text())
    assert notice["status"] == "pending"
    assert notice["attempts"] == 1
    await reports.deliver_pending()
    assert sender.await_count == 1
    notice["next_attempt_at"] = 0
    reports._save(path, notice)
    restored = CompletionNotifications(runner, sender)
    await restored.deliver_pending()
    assert sender.await_count == 2
    assert json.loads(path.read_text())["status"] == "sent"
    assert path.stat().st_mode & 0o077 == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,label",
    [
        ("failed", "执行失败"),
        ("timed_out", "已超时"),
        ("cancelled", "已取消"),
        ("interrupted", "监控已中断"),
    ],
)
async def test_unsuccessful_jobs_report_truthful_terminal_status(runner, status, label):
    """A failure, timeout or interruption cannot be presented as success."""
    sender = AsyncMock(return_value=True)
    reports = CompletionNotifications(runner, sender)
    job = await finished(runner, runner.start("owner", "partial output"))
    job.update(status=status, error="actual failure reason")
    runner._save(job)
    reports.register(job, "qq:FriendMessage:123")
    reports.release(job["job_id"])
    await reports.deliver_pending()
    message = sender.call_args.args[1].get_plain_text()
    assert label in message
    assert "actual failure reason" in message
    assert "已完成" not in message


@pytest.mark.asyncio
async def test_running_job_cannot_notify_before_full_finalization(runner, tmp_path):
    """A durable report waits for the task, then handles cancellation as a result."""
    sender = AsyncMock(return_value=True)
    reports = CompletionNotifications(runner, sender)
    job = runner.start("owner", "sleep")
    reports.register(job, "qq:FriendMessage:123")
    reports.release(job["job_id"])
    await reports.deliver_pending()
    sender.assert_not_awaited()
    await runner.cancel(job["job_id"], "owner")
    await reports.deliver_pending()
    sender.assert_awaited_once()


@pytest.mark.asyncio
async def test_recovered_waiting_notice_and_legacy_job_routing(runner):
    """A lost tool request recovers; an old job without an event route stays private."""
    sender = AsyncMock(return_value=True)
    reports = CompletionNotifications(runner, sender)
    job = await finished(runner, runner.start("owner", "final output"))
    await reports.deliver_pending()
    sender.assert_not_awaited()
    reports.register(job, "qq:FriendMessage:123")
    await reports.deliver_pending()
    sender.assert_not_awaited()
    path = runner.jobs_dir / job["job_id"] / "notification.json"
    notice = json.loads(path.read_text())
    notice["next_attempt_at"] = 0
    reports._save(path, notice)
    restored = CompletionNotifications(runner, sender)
    await restored.deliver_pending()
    sender.assert_awaited_once()


@pytest.mark.asyncio
async def test_background_notification_loop_does_not_need_another_user_message(runner):
    """Plugin initialization starts the sender once and unload stops it cleanly."""
    plugin = object.__new__(CodexCliPlugin)
    plugin.runner = runner
    sender = AsyncMock(return_value=True)
    plugin.notifications = CompletionNotifications(runner, sender)
    job = await finished(runner, runner.start("owner", "ready result"))
    plugin.notifications.register(job, "qq:FriendMessage:123")
    plugin.notifications.release(job["job_id"])
    await plugin.initialize()
    worker = plugin.notifications.task
    await plugin.initialize()
    assert plugin.notifications.task is worker
    for _ in range(100):
        if sender.await_count:
            break
        await asyncio.sleep(0.01)
    sender.assert_awaited_once()
    await plugin.terminate()
    assert worker.done()


@pytest.mark.asyncio
async def test_live_runner_upgrade_preserves_active_cli_worker(runner, tmp_path):
    """Replacing the runner class must preserve the current process and task map."""
    job = runner.start("owner", "sleep")
    for _ in range(100):
        if (tmp_path / "child.pid").exists():
            break
        await asyncio.sleep(0.02)
    process = runner.processes[job["job_id"]]
    worker = runner.tasks[job["job_id"]]
    spec = importlib.util.spec_from_file_location(
        "astrbot_plugin_codex_cli._upgrade_test",
        Path(__file__).parents[1] / "runner.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    runner.__class__ = module.CodexRunner
    runner.config["desktop_link"] = True
    assert process.returncode is None
    assert runner.tasks[job["job_id"]] is worker
    assert not worker.done()
    with pytest.raises(ValueError):
        runner.start("owner", "duplicate")
    assert (await runner.cancel(job["job_id"], "owner"))["status"] == "cancelled"


def test_workdir_symlink_cannot_escape_allowed_roots(runner, tmp_path):
    """Directory restrictions compare real paths instead of string prefixes."""
    root = tmp_path / "allowed"
    root.mkdir()
    (root / "escape").symlink_to(tmp_path, target_is_directory=True)
    runner.config["allowed_workdirs"] = [str(root)]
    with pytest.raises(ValueError):
        runner.start("owner", "task", str(root / "escape"))


@pytest.mark.parametrize(
    "private,admin,platform,allowed,permitted",
    [
        (True, True, "qq", [], True),
        (False, True, "qq", [], False),
        (True, False, "qq", [], False),
        (True, False, "qq", ["qq:123"], True),
        (True, False, "other", ["qq:123"], False),
    ],
)
def test_trusted_event_authorization(private, admin, platform, allowed, permitted):
    """Platform and private-chat authorization cannot be spoofed by tool input."""
    plugin = object.__new__(CodexCliPlugin)
    plugin.config = {"allowed_users": allowed}
    event = SimpleNamespace(
        is_private_chat=lambda: private,
        is_admin=lambda: admin,
        get_platform_id=lambda: platform,
        get_sender_id=lambda: "123",
        unified_msg_origin=platform + ":FriendMessage:123",
    )
    if permitted:
        assert len(plugin.owner(event)) == 64
    else:
        with pytest.raises(ValueError):
            plugin.owner(event)
