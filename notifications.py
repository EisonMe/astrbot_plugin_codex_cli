"""Persist and deliver completion reports independently of model tool calls."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import MessageChain

from .runner import ACTIVE, CodexRunner


class CompletionNotifications:
    """Keep report delivery state separate from worker-owned job metadata."""

    def __init__(self, runner: CodexRunner, send_message) -> None:
        """Bind the native message sender without retaining message events.

        Args:
            runner: Job storage and background workers.
            send_message: Native Context.send_message coroutine.
        """
        self.runner = runner
        self.send_message = send_message
        self.task = None
        self.wakeup = asyncio.Event()

    @staticmethod
    def _save(path: Path, notice: dict) -> None:
        """Atomically save routing and delivery status with private permissions.

        Args:
            path: Notification file under its existing job directory.
            notice: Trusted routing and delivery state.
        """
        temporary = path.with_suffix(".tmp")
        temporary.touch(mode=0o600, exist_ok=True)
        temporary.write_text(json.dumps(notice, ensure_ascii=False), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)

    def register(self, job: dict, origin: str) -> None:
        """Record the authorized event's route before yielding to the worker.

        Args:
            job: Newly created job with server-validated identity.
            origin: Original native event's unified message origin.
        """
        self._save(
            self.runner.jobs_dir / job["job_id"] / "notification.json",
            {
                "job_id": job["job_id"],
                "owner": job["owner"],
                "origin": origin,
                "status": "waiting",
                # Recover if a request or process dies before releasing its notice.
                "next_attempt_at": time.time() + 25,
                "attempts": 0,
            },
        )

    def release(self, job_id: str, inline: bool = False) -> None:
        """Arm background delivery or suppress a result already returned inline.

        Args:
            job_id: Newly registered job identifier.
            inline: Whether a terminal result was returned by the initial call.
        """
        path = self.runner.jobs_dir / job_id / "notification.json"
        notice = json.loads(path.read_text(encoding="utf-8"))
        notice.update(status="inline" if inline else "pending", next_attempt_at=0)
        self._save(path, notice)
        self.wakeup.set()

    async def deliver_pending(self) -> None:
        """Send due reports, persisting success and retrying transient failures.

        Old jobs with no trusted route are deliberately excluded. Successful
        delivery is persisted before the next scan; a crash between the platform
        send and that write may cause a retry because platform sends are not atomic.
        """
        for path in self.runner.jobs_dir.glob("*/notification.json"):
            try:
                notice = json.loads(path.read_text(encoding="utf-8"))
                if notice.get("status") in {"sent", "inline"}:
                    continue
                if notice.get("next_attempt_at", 0) > time.time():
                    continue
                job = self.runner.get(notice["job_id"], notice["owner"])
                if job["status"] in ACTIVE:
                    continue
                # Completion notifications must follow full worker finalization.
                if job["job_id"] in self.runner.tasks:
                    continue
                if path.parent.name != job["job_id"] or not notice.get("origin"):
                    continue
                label = {
                    "completed": "已完成",
                    "failed": "执行失败",
                    "timed_out": "已超时",
                    "cancelled": "已取消",
                    "interrupted": "监控已中断",
                }.get(job["status"], "状态已更新")
                text = f"Codex 任务{label}（{job['job_id']}）"
                if job.get("error"):
                    text += "\n\n" + str(job["error"])[:1000]
                result = str(job.get("result") or "")
                if result:
                    text += "\n\n" + result[:2800]
                if len(result) > 2800 or job.get("result_truncated"):
                    text += f"\n\n完整结果可发送 /codex status {job['job_id']} 查询。"
                notice.update(
                    status="sending",
                    attempts=int(notice.get("attempts", 0)) + 1,
                    next_attempt_at=time.time() + 30,
                )
                self._save(path, notice)
                try:
                    delivered = await asyncio.wait_for(
                        self.send_message(
                            notice["origin"], MessageChain().message(text)
                        ),
                        timeout=20,
                    )
                    if delivered is not True:
                        raise RuntimeError("The original platform is unavailable")
                except Exception:
                    notice.update(
                        status="pending",
                        next_attempt_at=time.time()
                        + min(300, 5 * 2 ** min(notice["attempts"], 6)),
                    )
                    self._save(path, notice)
                    logger.warning(
                        "Codex completion report failed for job %s; retry scheduled",
                        job["job_id"],
                    )
                    continue
                notice.update(status="sent", sent_at=time.time(), next_attempt_at=0)
                self._save(path, notice)
                logger.info("Codex completion report sent for job %s", job["job_id"])
            except (OSError, ValueError, KeyError, TypeError):
                logger.warning(
                    "Cannot read Codex completion notification %s", path.parent.name
                )

    async def run(self) -> None:
        """Poll persisted reports independently of any incoming user message."""
        while True:
            self.wakeup.clear()
            await self.deliver_pending()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.wakeup.wait(), timeout=2)

    async def close(self) -> None:
        """Stop polling while leaving pending notices available after a reload."""
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
