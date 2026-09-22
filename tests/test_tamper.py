"""哈希链防篡改：直接改写数据库中的旧判定会被审计发现。"""
import json
import unittest

from service.api import build_app
from tests.support import CROSS_BORDER_PROFILE


class TamperDetectionTest(unittest.TestCase):
    def test_retroactive_modification_is_detected(self):
        app = build_app(seed=True)
        app.releases.register("rel-1", CROSS_BORDER_PROFILE)
        app.queue.enqueue_for_current("rel-1", "t", app.rules)
        app.worker.run_pending()
        app.evidence.add("rel-1", "ev-1", "consent_notice", "补充")
        app.worker.run_pending()
        _, intact_before = app.store.verify_chain()
        self.assertTrue(intact_before)

        # 攻击者/误操作直接追溯改写旧批准结论
        app.store.execute(
            "UPDATE assessments SET decision='APPROVED' WHERE id=1")
        _, intact_after = app.store.verify_chain()
        self.assertFalse(intact_after)


if __name__ == "__main__":
    unittest.main()
