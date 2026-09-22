"""签署工作流：必要角色、利益冲突隔离、并发签署不跳过角色。"""
import json
import threading
import unittest
from datetime import datetime, timezone

from service.clock import FixedClock
from service.api import build_app
from service.errors import ConflictError, ValidationError
from tests.support import CROSS_BORDER_PROFILE


def make_app_ready():
    """构造一个补齐证据、等待签署的发布。"""
    clock = FixedClock(datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc))
    app = build_app(clock=clock, seed=True)
    app.releases.register("rel-1", CROSS_BORDER_PROFILE)
    app.queue.enqueue_for_current("rel-1", "test", app.rules)
    app.worker.run_pending()
    blocked = app.gate.decision("rel-1")
    kinds = {g["item"] for g in blocked["evidence_gaps"]
             if g["type"] == "evidence"}
    for kind in sorted(kinds):
        app.evidence.add("rel-1", f"ev-{kind}", kind, f"{kind} 文件")
    app.certificates.register(
        "cert-1", "中国国家网信部门", "cn_cross_border_data_transfer",
        "2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z",
        release_id="rel-1", jurisdiction="CN")
    app.worker.run_pending()
    result = app.gate.decision("rel-1")
    assert result["decision"] == "PENDING_SIGNOFF", result["decision"]
    return app, clock, result["assessment_id"], result["missing_roles"]


class SignoffTest(unittest.TestCase):
    def setUp(self):
        self.app, self.clock, self.assessment_id, self.roles = make_app_ready()
        self.app.reviewers.upsert("alice", [])
        self.app.reviewers.upsert("bob", [])
        self.app.reviewers.upsert("carol", ["CloudHostSG"])
        self.app.reviewers.upsert("dave", [])
        self.app.reviewers.upsert("erin", [])

    def test_all_required_roles_must_sign(self):
        # ai_governance_lead + business_owner（SG 高风险）
        # + privacy_officer（PDPA）+ data_compliance_officer（CN 出境）
        self.assertIn("ai_governance_lead", self.roles)
        self.app.signoffs.sign(
            "rel-1", self.assessment_id, "ai_governance_lead",
            "alice", "APPROVE")
        pending = self.app.gate.decision("rel-1")
        self.assertEqual(pending["decision"], "PENDING_SIGNOFF")
        self.assertNotIn("ai_governance_lead", pending["missing_roles"])
        self.assertIn("business_owner", pending["missing_roles"])

        self.app.signoffs.sign(
            "rel-1", self.assessment_id, "business_owner", "bob", "APPROVE")
        # 其余角色也签完（每人一个角色，职责隔离）
        for role, who in [("privacy_officer", "dave"),
                          ("data_compliance_officer", "erin")]:
            self.app.signoffs.sign(
                "rel-1", self.assessment_id, role, who, "APPROVE")
        approved = self.app.gate.decision("rel-1")
        self.assertEqual(approved["decision"], "APPROVED")
        self.assertTrue(approved["traffic_allowed"])

    def test_non_required_role_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.app.signoffs.sign(
                "rel-1", self.assessment_id, "marketing_lead",
                "alice", "APPROVE")

    def test_reviewer_conflict_is_isolated(self):
        # carol 与供应商 CloudHostSG 关联，不得签署
        with self.assertRaises(ConflictError) as ctx:
            self.app.signoffs.sign(
                "rel-1", self.assessment_id, "ai_governance_lead",
                "carol", "APPROVE")
        self.assertIn("利益", str(ctx.exception))

    def test_same_reviewer_cannot_hold_two_required_roles(self):
        self.app.signoffs.sign(
            "rel-1", self.assessment_id, "ai_governance_lead",
            "alice", "APPROVE")
        with self.assertRaises(ConflictError):
            self.app.signoffs.sign(
                "rel-1", self.assessment_id, "business_owner",
                "alice", "APPROVE")

    def test_duplicate_role_signature_rejected(self):
        self.app.signoffs.sign(
            "rel-1", self.assessment_id, "ai_governance_lead",
            "alice", "APPROVE")
        self.app.reviewers.upsert("dave", [])
        with self.assertRaises(ConflictError):
            self.app.signoffs.sign(
                "rel-1", self.assessment_id, "ai_governance_lead",
                "dave", "APPROVE")

    def test_rejection_blocks_approval(self):
        self.app.signoffs.sign(
            "rel-1", self.assessment_id, "ai_governance_lead",
            "alice", "REJECT", comment="证据不足")
        result = self.app.gate.decision("rel-1")
        self.assertEqual(result["decision"], "REJECTED")
        self.assertFalse(result["traffic_allowed"])

    def test_concurrent_signoffs_do_not_skip_or_duplicate_roles(self):
        roles = self.roles
        # 每个角色两名互不相同的竞争者，共 8 名审查者
        contenders = [(role, f"r-{role}-{k}") for role in roles
                      for k in (0, 1)]
        for _, reviewer in contenders:
            self.app.reviewers.upsert(reviewer, [])
        errors = []

        def sign(role, reviewer):
            try:
                self.app.signoffs.sign(
                    "rel-1", self.assessment_id, role, reviewer, "APPROVE")
            except Exception as exc:
                errors.append((role, reviewer, type(exc).__name__))

        threads = [threading.Thread(target=sign, args=pair)
                   for pair in contenders]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        rows = self.app.store.query(
            "SELECT role, COUNT(*) c FROM signoffs WHERE assessment_id=? "
            "GROUP BY role", (self.assessment_id,))
        counts = {r["role"]: r["c"] for r in rows}
        self.assertEqual(set(counts), set(roles))
        self.assertTrue(all(c == 1 for c in counts.values()))
        # 每个角色恰好一名落选者，失败原因均为重复角色冲突
        self.assertEqual(len(errors), len(roles))
        self.assertTrue(all(e[2] == "ConflictError" for e in errors))
        result = self.app.gate.decision("rel-1")
        self.assertEqual(result["decision"], "APPROVED")

    def test_cannot_sign_superseded_assessment(self):
        # 修订档案使旧判定过期
        self.app.releases.amend("rel-1",
                                dict(CROSS_BORDER_PROFILE,
                                     model_version="vision-v4.0"))
        self.app.queue.enqueue_for_current("rel-1", "amend", self.app.rules)
        self.app.worker.run_pending()
        with self.assertRaises(ConflictError):
            self.app.signoffs.sign(
                "rel-1", self.assessment_id, "ai_governance_lead",
                "alice", "APPROVE")


if __name__ == "__main__":
    unittest.main()
