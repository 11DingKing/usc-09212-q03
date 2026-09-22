"""临时豁免与外部证明：范围约束、期限约束、到期阻断新流量。"""
import unittest
from datetime import datetime, timedelta, timezone

from service.clock import FixedClock
from service.api import build_app
from service.errors import ValidationError
from tests.support import CROSS_BORDER_PROFILE


class WaiverTest(unittest.TestCase):
    def setUp(self):
        self.clock = FixedClock(datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc))
        self.app = build_app(clock=self.clock, seed=True)
        self.app.releases.register("rel-1", CROSS_BORDER_PROFILE)
        self.app.queue.enqueue_for_current("rel-1", "t", self.app.rules)
        self.app.worker.run_pending()
        self.obligation_id = "SG-PDPA-CONSENT-01"

    def test_waiver_requires_scope_note_and_valid_window(self):
        with self.assertRaises(ValidationError):
            self.app.waivers.grant(
                "w-1", "rel-1", "SG", self.obligation_id, "   ",
                "2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z")
        with self.assertRaises(ValidationError):
            self.app.waivers.grant(
                "w-1", "rel-1", "SG", self.obligation_id, "临时豁免",
                "2026-10-01T00:00:00Z", "2026-09-01T00:00:00Z")

    def test_waiver_is_scoped_to_obligation_and_limited_in_time(self):
        self.app.waivers.grant(
            "w-1", "rel-1", "SG", self.obligation_id,
            "监管窗口期间同意机制待补齐，限期整改",
            "2026-09-01T00:00:00Z", "2026-09-30T00:00:00Z")
        self.app.worker.run_pending()
        result = self.app.gate.decision("rel-1")
        waived = [o for o in result["obligations"]
                  if o["obligation_id"] == self.obligation_id][0]
        self.assertEqual(waived["status"], "WAIVED")
        self.assertEqual(waived["waiver_id"], "w-1")
        # 其它义务不受豁免影响
        self.assertTrue(any(o["status"] == "OUTSTANDING"
                            for o in result["obligations"]))

        # 豁免到期后自动失效并阻断
        self.clock.advance(timedelta(days=30))
        expired = self.app.gate.decision("rel-1")
        gaps = {(g["obligation_id"], g["type"]) for g in expired["evidence_gaps"]}
        self.assertIn((self.obligation_id, "waiver"), gaps)
        self.assertFalse(expired["traffic_allowed"])
        # 漂移检测安排了复评任务
        reasons = [j["reason"] for j in self.app.queue.list_jobs()]
        self.assertIn("gate_drift_detected", reasons)

    def test_revoked_waiver_blocks_traffic(self):
        self.app.waivers.grant(
            "w-1", "rel-1", "SG", self.obligation_id, "限期整改",
            "2026-09-01T00:00:00Z", "2026-12-01T00:00:00Z")
        self.app.worker.run_pending()
        self.app.waivers.revoke("w-1")
        self.app.worker.run_pending()
        result = self.app.gate.decision("rel-1")
        self.assertFalse(result["traffic_allowed"])
        # 撤销后已自动复评：义务回到未履行状态，出现普通证据缺口
        target = [o for o in result["obligations"]
                  if o["obligation_id"] == self.obligation_id][0]
        self.assertEqual(target["status"], "OUTSTANDING")
        self.assertTrue(any(g["obligation_id"] == self.obligation_id
                            for g in result["evidence_gaps"]))

    def test_waiver_does_not_leak_across_releases(self):
        self.app.waivers.grant(
            "w-1", "rel-1", "SG", self.obligation_id, "限期整改",
            "2026-09-01T00:00:00Z", "2026-12-01T00:00:00Z")
        self.app.releases.register("rel-2", CROSS_BORDER_PROFILE)
        self.app.queue.enqueue_for_current("rel-2", "t", self.app.rules)
        self.app.worker.run_pending()
        result = self.app.gate.decision("rel-2")
        target = [o for o in result["obligations"]
                  if o["obligation_id"] == self.obligation_id][0]
        self.assertEqual(target["status"], "OUTSTANDING")


class CertificateGateTest(unittest.TestCase):
    def setUp(self):
        self.clock = FixedClock(datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc))
        self.app = build_app(clock=self.clock, seed=True)
        self.app.releases.register("rel-1", CROSS_BORDER_PROFILE)
        self.app.queue.enqueue_for_current("rel-1", "t", self.app.rules)
        self.app.worker.run_pending()
        # 补齐全部普通证据
        blocked = self.app.gate.decision("rel-1")
        for kind in {g["item"] for g in blocked["evidence_gaps"]
                     if g["type"] == "evidence"}:
            self.app.evidence.add("rel-1", f"ev-{kind}", kind, "文件")

    def _approve_all(self, assessment_id):
        result = self.app.gate.decision("rel-1")
        for role in result["missing_roles"]:
            self.app.reviewers.upsert(f"rev-{role}", [])
            self.app.signoffs.sign(
                "rel-1", assessment_id, role, f"rev-{role}", "APPROVE")

    def test_expired_certificate_blocks_new_traffic_after_approval(self):
        # 外部证明 9 月 15 日到期
        self.app.certificates.register(
            "cert-1", "中国国家网信部门", "cn_cross_border_data_transfer",
            "2026-01-01T00:00:00Z", "2026-09-15T00:00:00Z",
            release_id="rel-1", jurisdiction="CN")
        self.app.worker.run_pending()
        pending = self.app.gate.decision("rel-1")
        self.assertEqual(pending["decision"], "PENDING_SIGNOFF")
        self._approve_all(pending["assessment_id"])
        approved = self.app.gate.decision("rel-1")
        self.assertEqual(approved["decision"], "APPROVED")

        # 到期前一天仍放行
        self.clock.advance(timedelta(days=13))
        self.assertEqual(self.app.gate.decision("rel-1")["decision"],
                         "APPROVED")

        # 到期时刻：阻断新流量，旧批准记录不被修改
        old_id = approved["assessment_id"]
        old_hash = approved["assessment_hash"]
        self.clock.advance(timedelta(days=1))
        blocked = self.app.gate.decision("rel-1")
        self.assertEqual(blocked["decision"], "BLOCKED")
        self.assertFalse(blocked["traffic_allowed"])
        drift = [g for g in blocked["evidence_gaps"]
                 if g["type"] == "certificate"]
        self.assertTrue(drift)
        self.assertTrue(drift[0]["reason"].startswith(
            "invalid_after_approval:expired"))
        old = self.app.store.query_one(
            "SELECT entry_hash FROM assessments WHERE id=?", (old_id,))
        self.assertEqual(old["entry_hash"], old_hash)

        # 更新证明并复评后恢复
        self.app.certificates.register(
            "cert-2", "中国国家网信部门", "cn_cross_border_data_transfer",
            "2026-09-15T00:00:00Z", "2027-09-15T00:00:00Z",
            release_id="rel-1", jurisdiction="CN")
        self.app.worker.run_pending()
        # 复评产生新判定，原角色签署在同一（最新）判定上，直接批准
        newest = self.app.gate.decision("rel-1")
        self.assertEqual(newest["decision"], "PENDING_SIGNOFF")
        self._approve_all(newest["assessment_id"])
        self.assertEqual(self.app.gate.decision("rel-1")["decision"],
                         "APPROVED")

    def test_revoked_certificate_blocks(self):
        self.app.certificates.register(
            "cert-1", "中国国家网信部门", "cn_cross_border_data_transfer",
            "2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z",
            release_id="rel-1", jurisdiction="CN")
        self.app.worker.run_pending()
        self.app.certificates.revoke("cert-1")
        self.app.worker.run_pending()
        result = self.app.gate.decision("rel-1")
        cert_gaps = [g for g in result["evidence_gaps"]
                     if g["type"] == "certificate"]
        self.assertTrue(cert_gaps)
        self.assertEqual(cert_gaps[0]["reason"], "revoked")

    def test_jurisdiction_wide_certificate_applies_within_scope(self):
        # 不绑定具体发布、限定辖区的通用证明
        self.app.certificates.register(
            "cert-j", "东盟数据跨境认证机构", "cn_cross_border_data_transfer",
            "2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z",
            release_id=None, jurisdiction="CN")
        self.app.worker.run_pending()
        result = self.app.gate.decision("rel-1")
        cert_gaps = [g for g in result["evidence_gaps"]
                     if g["type"] == "certificate"]
        self.assertEqual(cert_gaps, [])


if __name__ == "__main__":
    unittest.main()
