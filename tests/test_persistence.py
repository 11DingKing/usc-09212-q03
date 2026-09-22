"""持久化与重启恢复：评估队列跨进程继续、哈希链不被破坏。"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone

from service.clock import FixedClock
from service.api import build_app
from service.engine import PENDING, RUNNING
from tests.support import CROSS_BORDER_PROFILE


class RestartRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "compliance.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_queue_and_ledger_survive_restart(self):
        clock = FixedClock(datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc))
        app = build_app(self.db_path, clock=clock, seed=True)
        app.releases.register("rel-1", CROSS_BORDER_PROFILE)
        app.enqueue_after_register("rel-1", "release_registered")
        job_id = app.queue.list_jobs()[-1]["id"]
        app.store.close()

        # 模拟重启：新应用实例打开同一数据库文件
        app2 = build_app(self.db_path, clock=clock, seed=False)
        jobs = app2.queue.list_jobs()
        self.assertTrue(any(j["id"] == job_id and j["status"] == PENDING
                            for j in jobs))
        # 重启后工作线程恢复并完成评估
        app2.start_worker()
        try:
            deadline = 50
            while deadline and app2.queue.list_jobs()[-1]["status"] == PENDING:
                import time
                time.sleep(0.05)
                deadline -= 1
        finally:
            app2.stop_worker()
        self.assertEqual(app2.queue.list_jobs()[-1]["status"], "done")
        result = app2.gate.decision("rel-1")
        self.assertEqual(result["decision"], "BLOCKED")
        count, intact = app2.store.verify_chain()
        self.assertEqual(count, 1)
        self.assertTrue(intact)
        app2.store.close()

    def test_running_job_is_recovered_as_pending(self):
        clock = FixedClock(datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc))
        app = build_app(self.db_path, clock=clock, seed=True)
        app.releases.register("rel-1", CROSS_BORDER_PROFILE)
        job_id = app.queue.enqueue(
            "rel-1", 1, app.rules.active_version(), "crash_mid_eval")
        # 模拟进程在 RUNNING 状态崩溃
        app.queue.lease()
        self.assertEqual(app.queue.list_jobs()[-1]["status"], RUNNING)
        app.store.close()

        app2 = build_app(self.db_path, clock=clock, seed=False)
        job = [j for j in app2.queue.list_jobs() if j["id"] == job_id][0]
        self.assertEqual(job["status"], PENDING)
        self.assertEqual(job["last_error"], "recovered_after_restart")
        app2.worker.run_pending()
        self.assertEqual(app2.queue.list_jobs()[-1]["status"], "done")
        app2.store.close()


if __name__ == "__main__":
    unittest.main()
