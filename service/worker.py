"""评估队列 worker：后台轮询持久队列，重启后经 recover_queue 续跑。"""
from __future__ import annotations

import threading

from .core import ComplianceService

MAX_ATTEMPTS = 3


class QueueWorker:
    def __init__(self, service: ComplianceService, interval: float = 0.2):
        self.service = service
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def process_one(self) -> bool:
        """处理一个待办任务，返回是否有任务被处理。"""
        job = self.service.store.claim_job()
        if job is None:
            return False
        try:
            if job["kind"] == "evaluation":
                self.service.run_evaluation(job["payload"]["eval_id"])
            else:
                raise ValueError(f"未知任务类型: {job['kind']}")
        except Exception as exc:  # noqa: BLE001 - 失败任务须回队或标记
            self.service.store.fail_job(
                job["job_id"], str(exc), retry=job["attempts"] < MAX_ATTEMPTS)
            return True
        self.service.store.complete_job(job["job_id"])
        return True

    def run_until_idle(self) -> int:
        """同步排空队列（测试与启动恢复使用），返回处理条数。"""
        processed = 0
        while self.process_one():
            processed += 1
        return processed

    def _loop(self):
        while not self._stop.is_set():
            if not self.process_one():
                self._stop.wait(self.interval)

    def start(self):
        self.service.store.recover_queue()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
