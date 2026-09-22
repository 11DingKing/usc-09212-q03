"""HTTP 端到端：从注册发布到批准放行的完整判定接口流程。"""
import json
import unittest

from tests.support import ApiServer, CROSS_BORDER_PROFILE


class ApiFlowTest(unittest.TestCase):
    def setUp(self):
        self.server = ApiServer(seed=True)

    def tearDown(self):
        self.server.stop()

    def test_full_flow_via_http(self):
        # 1. 注册发布 -> 自动入队首次评估
        status, body = self.server.post("/releases", dict(
            id="rel-http", **CROSS_BORDER_PROFILE))
        self.assertEqual(status, 201)
        self.assertEqual(body["release"]["latest_revision"], 1)
        self.assertIsNotNone(body["enqueued_job_id"])

        # 2. 判定接口返回具体义务与证据缺口
        self.server.drain()
        status, decision = self.server.get("/releases/rel-http/decision")
        self.assertEqual(status, 200)
        self.assertEqual(decision["decision"], "BLOCKED")
        self.assertFalse(decision["traffic_allowed"])
        self.assertTrue(decision["obligations"])
        self.assertTrue(decision["evidence_gaps"])
        gap = decision["evidence_gaps"][0]
        self.assertTrue({"obligation_id", "jurisdiction", "type", "item",
                         "reason"} <= set(gap))
        # 可复核判定路径：锁定档案、规则、命中条件、哈希锚点
        steps = [p["step"] for p in decision["decision_path"]]
        self.assertEqual(steps[0], "profile_locked")
        self.assertEqual(decision["decision_path"][-1]["step"],
                         "live_gate_checks")

        # 3. 补齐证据与证明
        kinds = {g["item"] for g in decision["evidence_gaps"]
                 if g["type"] == "evidence"}
        for kind in sorted(kinds):
            status, _ = self.server.post("/evidence", {
                "id": f"ev-{kind}", "release_id": "rel-http",
                "kind": kind, "description": f"{kind} 文件"})
            self.assertEqual(status, 201)
        status, _ = self.server.post("/certificates", {
            "id": "cert-1",
            "issuer": "中国国家网信部门",
            "scope": "cn_cross_border_data_transfer",
            "valid_from": "2026-01-01T00:00:00Z",
            "valid_until": "2027-01-01T00:00:00Z",
            "release_id": "rel-http", "jurisdiction": "CN"})
        self.assertEqual(status, 201)
        self.server.drain()

        # 4. 进入签署阶段；登记审查者
        status, pending = self.server.get("/releases/rel-http/decision")
        self.assertEqual(pending["decision"], "PENDING_SIGNOFF")
        assessment_id = pending["assessment_id"]
        roles = pending["missing_roles"]
        for role in roles:
            self.server.post("/reviewers",
                             {"reviewer_id": f"officer-{role}",
                              "affiliations": []})

        # 5. 并发签署不可跳过角色：缺一个角色仍不放行
        for role in roles[:-1]:
            status, _ = self.server.post("/releases/rel-http/signoffs", {
                "assessment_id": assessment_id, "role": role,
                "reviewer_id": f"officer-{role}", "decision": "APPROVE"})
            self.assertEqual(status, 201)
        status, partial = self.server.get("/releases/rel-http/decision")
        self.assertEqual(partial["decision"], "PENDING_SIGNOFF")
        self.assertEqual(partial["missing_roles"], [roles[-1]])

        # 利益冲突审查者被拒
        self.server.post("/reviewers",
                         {"reviewer_id": "conflicted",
                          "affiliations": ["LabelCoID"]})
        status, err = self.server.post("/releases/rel-http/signoffs", {
            "assessment_id": assessment_id, "role": roles[-1],
            "reviewer_id": "conflicted", "decision": "APPROVE"})
        self.assertEqual(status, 409)
        self.assertIn("利益", err["error"]["message"])

        # 6. 无冲突审查者签完 -> 放行
        status, _ = self.server.post("/releases/rel-http/signoffs", {
            "assessment_id": assessment_id, "role": roles[-1],
            "reviewer_id": f"officer-{roles[-1]}", "decision": "APPROVE"})
        self.assertEqual(status, 201)
        status, approved = self.server.get("/releases/rel-http/decision")
        self.assertEqual(approved["decision"], "APPROVED")
        self.assertTrue(approved["traffic_allowed"])

        # 7. 哈希链完整、判定记录可查
        status, audit = self.server.get("/audit/chain")
        self.assertTrue(audit["chain_intact"])
        self.assertGreaterEqual(audit["assessment_count"], 1)
        status, detail = self.server.get(
            f"/assessments/{assessment_id}")
        self.assertEqual(status, 200)
        self.assertEqual(detail["input_refs"]["release_id"], "rel-http")
        self.assertTrue(detail["entry_hash"])

    def test_rule_update_returns_affected_releases(self):
        self.server.post("/releases", dict(id="rel-1", **CROSS_BORDER_PROFILE))
        self.server.drain()
        new_package = json.loads(json.dumps(__import__(
            "service.seed_rules", fromlist=["SEED_PACKAGE"]).SEED_PACKAGE))
        new_package["version"] = "2026.12"
        new_package["jurisdictions"]["SG"]["rules"][0]["obligation"][
            "required_roles"].append("external_auditor")
        status, body = self.server.post("/rules", new_package)
        self.assertEqual(status, 201)
        self.assertIn("rel-1", body["affected_releases"])
        self.server.drain()
        status, decision = self.server.get("/releases/rel-1/decision")
        self.assertEqual(decision["rule_version"], "2026.12")

    def test_unknown_release_returns_404(self):
        status, body = self.server.get("/releases/nope/decision")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
