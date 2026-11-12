"""Run durable, bounded Codex CLI jobs without blocking AstrBot tool calls."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import signal
import time
import uuid
from pathlib import Path
from typing import Any

from astrbot.core.utils.error_redaction import redact_sensitive_text

from .app_server import AppServerClient

ACTIVE = {"queued", "running"}


class CodexRunner:
    """Own subprocesses and persist job metadata separately from CLI sessions."""

    def __init__(self, data_dir: Path, config: dict) -> None:
        """Prepare storage and mark jobs from an earlier process as interrupted.

        Args:
            data_dir: Dedicated plugin data directory.
            config: Trusted plugin configuration, never model-generated flags.
        """
        self.config = config
        self.jobs_dir = data_dir / "jobs"
        self.jobs_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.tasks: dict[str, asyncio.Task] = {}
        self.processes: dict[str, asyncio.subprocess.Process] = {}
        self.stopping = False
        for path in self.jobs_dir.glob("*/job.json"):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
                if job.get("status") in ACTIVE:
                    job.update(status="interrupted", finished_at=time.time())
                    if job.get("desktop_link"):
                        job["error"] = (
                            "AstrBot 监控连接已中断；Codex 应用任务可能仍在运行，"
                            "请在桌面确认。继续同一任务时会先检查是否仍在执行。"
                        )
                    self._save(job)
            except (OSError, ValueError, KeyError):
                continue

    def _save(self, job: dict) -> None:
        """Atomically persist job metadata with owner-only file permissions.

        Args:
            job: Job state containing a validated generated identifier.
        """
        path = self.jobs_dir / job["job_id"] / "job.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)

    def get(self, job_id: str, owner: str) -> dict:
        """Read a job after checking its identifier and session ownership.

        Args:
            job_id: Opaque plugin job ID; empty selects this owner's latest job.
            owner: Server-derived session identity.

        Returns:
            Stored job metadata.

        Raises:
            ValueError: If the job is missing, invalid, or belongs to another owner.
        """
        if not job_id:
            candidates = []
            for path in self.jobs_dir.glob("*/job.json"):
                with contextlib.suppress(OSError, ValueError):
                    job = json.loads(path.read_text(encoding="utf-8"))
                    if job.get("owner") == owner:
                        candidates.append(job)
            if not candidates:
                raise ValueError("当前会话还没有 Codex 任务。")
            return max(candidates, key=lambda job: job["created_at"])
        if not re.fullmatch(r"[a-f0-9]{12}", job_id):
            raise ValueError("Codex 任务编号无效。")
        try:
            job = json.loads(
                (self.jobs_dir / job_id / "job.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise ValueError("找不到当前会话的 Codex 任务。") from exc
        if job.get("owner") != owner:
            raise ValueError("找不到当前会话的 Codex 任务。")
        return job

    def start(
        self, owner: str, prompt: str, workdir: str = "", resume_job_id: str = ""
    ) -> dict:
        """Validate a request and schedule a subprocess outside the tool lifetime.

        Args:
            owner: Server-derived session identity.
            prompt: Task sent verbatim over stdin, never interpolated into a shell.
            workdir: Existing working directory, or the configured default.
            resume_job_id: Earlier job in this session whose CLI thread to resume.

        Returns:
            Initial job metadata; execution continues independently.

        Raises:
            ValueError: If configuration, request, or concurrency checks fail.
        """
        if self.stopping:
            raise ValueError("Codex 插件正在停止，请稍后重试。")
        if not prompt.strip() or len(prompt) > 30000 or "\x00" in prompt:
            raise ValueError("任务内容不能为空，且不能超过 30000 字符。")
        limit = max(1, min(8, int(self.config.get("max_concurrent_jobs", 1))))
        if len(self.tasks) >= limit:
            raise ValueError("Codex 已有任务正在执行，请先查询进度或等待完成。")
        previous = self.get(resume_job_id, owner) if resume_job_id else None
        if previous and (
            previous["status"] in ACTIVE or not previous.get("session_id")
        ):
            raise ValueError("该任务尚未结束或没有可恢复的 Codex 会话。")
        desktop_link = bool(self.config.get("desktop_link", False))
        if previous and previous.get("desktop_link") and not desktop_link:
            raise ValueError("桌面任务必须通过桌面联动继续，请先启用 desktop_link。")
        directory = (
            Path(
                workdir
                or (previous or {}).get("workdir")
                or self.config.get("default_workdir")
                or Path.cwd()
            )
            .expanduser()
            .resolve()
        )
        if not directory.is_dir():
            raise ValueError("工作目录不存在或不是文件夹。")
        roots = self.config.get("allowed_workdirs", [])
        if roots and not any(
            directory.is_relative_to(Path(root).expanduser().resolve())
            for root in roots
        ):
            raise ValueError("工作目录不在配置允许的范围内。")
        executable = shutil.which(str(self.config.get("codex_path") or "codex"))
        if not executable:
            raise ValueError("没有找到 Codex CLI，请检查插件的 codex_path 配置。")
        environment = os.environ.copy()
        env_file = self.config.get("environment_file")
        if env_file:
            try:
                extra = json.loads(Path(env_file).expanduser().read_text("utf-8"))
                if not isinstance(extra, dict) or not all(
                    isinstance(key, str)
                    and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
                    and isinstance(value, str)
                    and "\x00" not in value
                    for key, value in extra.items()
                ):
                    raise ValueError
                environment.update(extra)
            except (OSError, ValueError) as exc:
                raise ValueError(
                    "无法读取 Codex 环境变量 JSON 文件，请检查本地配置。"
                ) from exc
        for key in (
            "CODEX_THREAD_ID",
            "CODEX_SESSION_ID",
            "CODEX_REMOTE_PAYLOAD",
            "CODEX_TOOL_CALL_ID",
            "CODEX_APPLY_PATCH_PRESERVE_LINE_ENDINGS",
        ):
            environment.pop(key, None)
        environment.update(NO_COLOR="1", TERM="dumb")
        secrets = [
            value
            for key, value in environment.items()
            if len(value) >= 8
            and any(
                part in key.upper() for part in ("KEY", "TOKEN", "SECRET", "PASSWORD")
            )
        ]
        args = [executable, "-c", 'approval_policy="never"']
        sandbox = str(self.config.get("sandbox", "inherit"))
        if sandbox not in {
            "inherit",
            "read-only",
            "workspace-write",
            "danger-full-access",
        }:
            raise ValueError("Codex sandbox 配置无效。")
        if sandbox != "inherit":
            args.extend(["-c", "sandbox_mode=" + json.dumps(sandbox)])
        job_id = uuid.uuid4().hex[:12]
        job_dir = self.jobs_dir / job_id
        output = job_dir / "result.txt"
        args.append("exec")
        if previous:
            args.append("resume")
        args.extend(["--json", "--skip-git-repo-check", "-o", str(output)])
        model = str(self.config.get("model") or "").strip()
        if model:
            args.extend(["--model", model])
        if previous:
            args.append(str(uuid.UUID(previous["session_id"])))
        args.append("-")
        job_dir.mkdir(mode=0o700)
        output.touch(mode=0o600)
        job = {
            "job_id": job_id,
            "owner": owner,
            "status": "queued",
            "created_at": time.time(),
            "workdir": str(directory),
            "resume_job_id": resume_job_id,
            "progress": "等待启动",
            "result": "",
            "error": "",
            "exit_code": None,
            "desktop_link": desktop_link,
            "session_id": (previous or {}).get("session_id", "")
            if desktop_link
            else "",
        }
        self._save(job)
        task = asyncio.create_task(
            self._run_app(job, prompt, secrets)
            if desktop_link
            else self._run(job, args, prompt, environment, secrets),
            name="codex-" + job_id,
        )
        self.tasks[job_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(job_id, None))
        return job

    async def _run_app(self, job: dict, prompt: str, secrets: list[str]) -> None:
        """Start and monitor a turn owned by the desktop's shared Codex daemon.

        Args:
            job: Mutable metadata owned by this worker.
            prompt: Original task text passed as a structured user input.
            secrets: Credentials to redact from plugin logs and status results.
        """
        done = asyncio.Event()
        last_saved = 0.0
        log_size = 0
        final_message = ""
        started = False
        terminal = False
        log_path = self.jobs_dir / job["job_id"] / "events.jsonl"
        log_path.touch(mode=0o600)

        def clean(text: str) -> str:
            """Redact captured text before saving it in plugin storage.

            Args:
                text: Captured app-server output.

            Returns:
                Redacted text.
            """
            for secret in secrets:
                text = text.replace(secret, "[REDACTED]")
            return redact_sensitive_text(text)

        def on_event(event: dict) -> None:
            """Monitor only the exact thread and turn assigned to this job.

            Args:
                event: A notification from the shared server.
            """
            nonlocal last_saved, log_size, final_message, terminal
            params = event.get("params") or {}
            if params.get("threadId") != job.get("session_id"):
                return
            method = event.get("method", "")
            if not started and method.startswith(("turn/", "item/")):
                return
            turn_id = params.get("turnId") or (params.get("turn") or {}).get("id")
            if turn_id and job.get("turn_id") and turn_id != job["turn_id"]:
                return
            if method == "turn/started":
                job["turn_id"] = params["turn"]["id"]
            line = clean(json.dumps(event, ensure_ascii=False)) + "\n"
            if log_size < 2_000_000:
                with log_path.open("a", encoding="utf-8") as file:
                    file.write(line[: 2_000_000 - log_size])
                log_size += len(line)
            item = params.get("item") or {}
            if method in {"item/started", "item/completed"}:
                kind = item.get("type", "")
                job["progress"] = {
                    "reasoning": "正在分析任务",
                    "commandExecution": "执行命令："
                    + str(item.get("command", ""))[:350],
                    "fileChange": "正在修改文件",
                    "mcpToolCall": "调用工具：" + str(item.get("tool", ""))[:150],
                }.get(kind, kind)
                if kind == "agentMessage" and item.get("text"):
                    message = clean(str(item["text"]))
                    job["progress"] = message[:500]
                    if method == "item/completed" and item.get("phase") in {
                        None,
                        "final_answer",
                    }:
                        final_message = message[:50001]
            elif method == "item/agentMessage/delta":
                item_id = params.get("itemId")
                if item_id != job.get("live_item_id"):
                    job.update(live_item_id=item_id, live_message="")
                job["live_message"] = clean(
                    job.get("live_message", "") + str(params.get("delta", ""))
                )[-2000:]
                job["progress"] = job["live_message"][-500:]
            elif method == "thread/status/changed":
                job["desktop_status"] = (params.get("status") or {}).get("type")
            elif method == "thread/tokenUsage/updated":
                job["usage"] = (params.get("tokenUsage") or {}).get("last", {})
            elif method == "turn/completed":
                turn = params.get("turn") or {}
                status = turn.get("status")
                terminal = True
                job["result_truncated"] = len(final_message) > 50000
                job["result"] = final_message[:50000]
                if status == "completed" and final_message:
                    job.update(status="completed", error="", exit_code=0)
                elif status == "interrupted":
                    if job["status"] in ACTIVE:
                        job.update(status="cancelled", error="Codex 应用任务已停止。")
                else:
                    error = turn.get("error") or {}
                    job.update(
                        status="failed",
                        error=clean(
                            str(error.get("message") or "Codex 未返回有效的最终结果。")
                        )[:2000],
                    )
                done.set()
            if done.is_set() or time.monotonic() - last_saved > 1:
                self._save(job)
                last_saved = time.monotonic()

        client = AppServerClient(
            str(self.config.get("app_server_socket") or ""), on_event
        )
        connection_wait = None
        completion_wait = None
        try:
            timeout = max(5, min(86400, int(self.config.get("timeout_seconds", 3600))))
            await client.connect()
            params = {"cwd": job["workdir"], "approvalPolicy": "never"}
            model = str(self.config.get("model") or "").strip()
            if model:
                params["model"] = model
            sandbox = self.config.get("sandbox", "inherit")
            if sandbox != "inherit":
                params["sandbox"] = sandbox
            if job["session_id"]:
                # A metadata read does not load or change an independently running CLI session.
                current = await client.request(
                    "thread/read", {"threadId": job["session_id"]}
                )
                if current["thread"].get("status", {}).get("type") == "active":
                    raise ValueError(
                        "该 Codex 会话仍在桌面执行，请等待完成，避免重复启动。"
                    )
                params.update(threadId=job["session_id"], excludeTurns=True)
                response = await client.request("thread/resume", params)
            else:
                params.update(ephemeral=False, historyMode="paginated")
                response = await client.request("thread/start", params)
            job["session_id"] = str(uuid.UUID(response["thread"]["id"]))
            job["desktop_status"] = response["thread"].get("status", {}).get("type")
            if job["desktop_status"] == "active":
                raise ValueError("该 Codex 会话仍在执行，请等待完成。")
            prefix = str(self.config.get("task_title_prefix") or "AstrBot").strip()[:40]
            job["app_thread_name"] = (
                clean(prefix) + " · " + " ".join(clean(prompt).split())[:65]
            )
            await client.request(
                "thread/name/set",
                {"threadId": job["session_id"], "name": job["app_thread_name"]},
            )
            job.update(
                status="running",
                started_at=time.time(),
                progress="已连接 Codex 应用，正在启动",
            )
            self._save(job)
            # Never retry this side effect: a lost response may still mean the turn started.
            started = True
            response = await client.request(
                "turn/start",
                {
                    "threadId": job["session_id"],
                    "input": [{"type": "text", "text": prompt}],
                    "approvalPolicy": "never",
                },
            )
            job["turn_id"] = response["turn"]["id"]
            self._save(job)
            completion_wait = asyncio.create_task(done.wait())
            connection_wait = asyncio.create_task(client.closed.wait())
            completed, _ = await asyncio.wait(
                {completion_wait, connection_wait},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not completed:
                raise TimeoutError
            if not terminal:
                raise RuntimeError("Codex 应用连接已断开，任务状态需要确认。")
        except asyncio.CancelledError:
            if not terminal:
                job.update(status="cancelled", error="Codex 任务已取消。")
        except TimeoutError:
            if not terminal:
                job.update(status="timed_out", error="Codex 任务已到达配置的运行时限。")
        except Exception as exc:
            if not terminal:
                job.update(status="failed", error=clean(str(exc))[:2000])
        finally:
            if started and not terminal:
                try:
                    if client.closed.is_set():
                        await client.close()
                        client = AppServerClient(
                            str(self.config.get("app_server_socket") or ""), on_event
                        )
                        await client.connect()
                    # Never guess a turn ID: a competing desktop turn is not ours to stop.
                    if not job.get("turn_id"):
                        raise RuntimeError("The owned turn ID could not be confirmed")
                    await client.request(
                        "turn/interrupt",
                        {"threadId": job["session_id"], "turnId": job["turn_id"]},
                    )
                except Exception:
                    job.update(
                        status="interrupted",
                        error="与 Codex 应用连接断开，未能确认任务已停止，请在桌面检查；不会自动重试。",
                    )
            for waiter in (completion_wait, connection_wait):
                if waiter is not None:
                    waiter.cancel()
                    await asyncio.gather(waiter, return_exceptions=True)
            await client.close()
            result_path = self.jobs_dir / job["job_id"] / "result.txt"
            result_path.write_text(job["result"], encoding="utf-8")
            result_path.chmod(0o600)
            job["finished_at"] = time.time()
            self._save(job)

    async def _run(
        self,
        job: dict,
        args: list[str],
        prompt: str,
        environment: dict,
        secrets: list[str],
    ) -> None:
        """Drain JSONL and stderr concurrently and finalize each terminal state.

        Args:
            job: Mutable metadata owned by this worker.
            args: Already validated argument vector.
            prompt: Task passed only through stdin.
            environment: Local CLI environment including configured credentials.
            secrets: Known values to redact from saved logs and tool output.
        """
        process = None
        readers: list[asyncio.Task] = []
        stderr_tail = ""
        last_agent_message = ""
        log_size = 0
        last_saved = 0.0
        turn_failed = False
        job_dir = self.jobs_dir / job["job_id"]
        log_path = job_dir / "events.jsonl"
        log_path.touch(mode=0o600)

        def clean(text: str) -> str:
            """Redact known credentials and common secret representations.

            Args:
                text: Captured CLI output.

            Returns:
                Text suitable for plugin storage and tool results.
            """
            for secret in secrets:
                text = text.replace(secret, "[REDACTED]")
            return redact_sensitive_text(text)

        async def drain(stream: asyncio.StreamReader, stderr: bool = False) -> None:
            """Consume one pipe while recording bounded output and progress.

            Args:
                stream: Child process output pipe.
                stderr: Whether the pipe contains diagnostics instead of JSONL.
            """
            nonlocal stderr_tail, last_agent_message, log_size, last_saved, turn_failed
            while chunk := await stream.readline():
                line = clean(chunk.decode("utf-8", errors="replace"))
                if log_size < 2_000_000:
                    with log_path.open("a", encoding="utf-8") as file:
                        file.write(line[: max(0, 2_000_000 - log_size)])
                    log_size += len(line)
                if stderr:
                    stderr_tail = (stderr_tail + line)[-4000:]
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                kind = event.get("type", "")
                item = event.get("item") or {}
                if kind == "thread.started":
                    job["session_id"] = str(event.get("thread_id") or "")
                elif kind.startswith("item.") and isinstance(item, dict):
                    job["progress"] = str(item.get("type") or kind)
                    if item.get("type") == "agent_message" and item.get("text"):
                        last_agent_message = str(item["text"])[-50000:]
                        job["progress"] = last_agent_message[:500]
                elif kind == "turn.completed":
                    job["usage"] = event.get("usage", {})
                elif kind in {"error", "turn.failed"}:
                    turn_failed = kind == "turn.failed" or turn_failed
                    detail = event.get("error") or event
                    job["error"] = (
                        str(detail.get("message", "Codex execution failed"))[:2000]
                        if isinstance(detail, dict)
                        else str(detail)[:2000]
                    )
                if time.monotonic() - last_saved > 1:
                    self._save(job)
                    last_saved = time.monotonic()

        try:
            timeout = max(5, min(86400, int(self.config.get("timeout_seconds", 3600))))
            process = await asyncio.create_subprocess_exec(
                *args,
                cwd=job["workdir"],
                env=environment,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=os.name == "posix",
                limit=4 * 1024 * 1024,
            )
            self.processes[job["job_id"]] = process
            job.update(status="running", started_at=time.time())
            self._save(job)
            readers = [
                asyncio.create_task(drain(process.stdout)),
                asyncio.create_task(drain(process.stderr, stderr=True)),
            ]
            process.stdin.write(prompt.encode("utf-8"))
            await asyncio.wait_for(process.stdin.drain(), timeout=timeout)
            process.stdin.close()
            await asyncio.wait_for(
                asyncio.gather(*readers, process.wait()), timeout=timeout
            )
            job["exit_code"] = process.returncode
            result_path = job_dir / "result.txt"
            with result_path.open("r", encoding="utf-8", errors="replace") as file:
                result = clean(file.read(50001)).strip() or last_agent_message
            job["result_truncated"] = len(result) > 50000
            result = result[:50000]
            result_path.write_text(result, encoding="utf-8")
            result_path.chmod(0o600)
            job["result"] = result
            if process.returncode == 0 and result and not turn_failed:
                job.update(status="completed", error="")
            else:
                job.update(
                    status="failed",
                    error=job["error"] or stderr_tail or "Codex 未返回有效结果。",
                )
        except TimeoutError:
            job.update(status="timed_out", error="Codex 任务已到达配置的运行时限。")
        except asyncio.CancelledError:
            job.update(status="cancelled", error="Codex 任务已取消。")
        except Exception as exc:
            job.update(status="failed", error=clean(str(exc))[:2000])
        finally:
            if process:
                # Kill the process group even if the leader has already exited.
                if os.name == "posix":
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGTERM)
                elif process.returncode is None:
                    process.terminate()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(process.wait(), timeout=2)
                if os.name == "posix":
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                elif process.returncode is None:
                    process.kill()
                await process.wait()
            for reader in readers:
                if not reader.done():
                    reader.cancel()
            if readers:
                await asyncio.gather(*readers, return_exceptions=True)
            self.processes.pop(job["job_id"], None)
            if process:
                job["exit_code"] = process.returncode
            if not job["result"]:
                result_path = job_dir / "result.txt"
                with result_path.open("r", encoding="utf-8", errors="replace") as file:
                    partial = clean(file.read(50001)).strip() or last_agent_message
                job["result_truncated"] = len(partial) > 50000
                job["result"] = partial[:50000]
                result_path.write_text(job["result"], encoding="utf-8")
                result_path.chmod(0o600)
            job["finished_at"] = time.time()
            self._save(job)

    async def cancel(self, job_id: str, owner: str) -> dict:
        """Cancel only a job belonging to the calling session.

        Args:
            job_id: Current job identifier, or empty for the most recent job.
            owner: Server-derived session identity.

        Returns:
            Final job metadata.
        """
        job = self.get(job_id, owner)
        task = self.tasks.get(job["job_id"])
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            # A task cancelled before its first instruction cannot run its finally.
            current = self.get(job["job_id"], owner)
            if current["status"] in ACTIVE:
                current.update(status="cancelled", finished_at=time.time())
                self._save(current)
        return self.get(job["job_id"], owner)

    async def close(self) -> None:
        """Stop active jobs during plugin unload without orphaning subprocesses.

        Returns:
            None.
        """
        self.stopping = True
        for job_id in list(self.tasks):
            path = self.jobs_dir / job_id / "job.json"
            job = json.loads(path.read_text(encoding="utf-8"))
            await self.cancel(job_id, job["owner"])

    @staticmethod
    def view(job: dict, offset: int = 0, limit: int = 6000) -> dict[str, Any]:
        """Return a bounded page without leaking the job owner or credentials.

        Args:
            job: Stored metadata after ownership validation.
            offset: Character offset into the final result.
            limit: Maximum result characters for this page.

        Returns:
            Public status with a cursor when more result text is available.
        """
        offset = max(0, int(offset))
        limit = max(500, min(16000, int(limit)))
        result = job.get("result", "")
        end = min(offset + limit, len(result))
        return {
            **{
                key: job.get(key)
                for key in (
                    "job_id",
                    "status",
                    "workdir",
                    "session_id",
                    "resume_job_id",
                    "created_at",
                    "started_at",
                    "finished_at",
                    "exit_code",
                    "usage",
                    "progress",
                    "error",
                    "desktop_link",
                    "desktop_status",
                    "app_thread_name",
                    "turn_id",
                )
            },
            "result": result[offset:end],
            "result_length": len(result),
            "result_truncated": job.get("result_truncated", False),
            "next_offset": end if end < len(result) else None,
        }
