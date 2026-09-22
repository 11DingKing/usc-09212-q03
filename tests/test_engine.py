"""规则引擎单元测试。"""
import unittest

from service import engine


PROFILE = {
    "model_version": "m-1.0.0",
    "owner_org": "OrgA",
    "data_categories": ["personal", "minors"],
    "purposes": ["credit_scoring"],
    "data_origin_country": "CN",
    "deployment_country": "SG",
    "vendor_chain": [{"org": "V1", "role": "hosting"},
                     {"org": "V2", "role": "analytics"},
                     {"org": "V3", "role": "support"}],
    "evidence": [{"id": "ev.cn.security_assessment_report"}],
}

NOW = "2026-09-22T00:00:00+00:00"


class MatchConditionsTest(unittest.TestCase):
    def test_data_categories_and_purposes(self):
        matched, _ = engine.match_conditions(
            {"data_categories_any": ["personal"]}, PROFILE)
        self.assertTrue(matched)
        matched, reasons = engine.match_conditions(
            {"purposes_any": ["chat"]}, PROFILE)
        self.assertFalse(matched)
        self.assertTrue(any("用途" in r for r in reasons))

    def test_cross_border(self):
        matched, _ = engine.match_conditions({"cross_border": True}, PROFILE)
        self.assertTrue(matched)
        local = {**PROFILE, "deployment_country": "CN"}
        matched, _ = engine.match_conditions({"cross_border": True}, local)
        self.assertFalse(matched)

    def test_vendor_chain_length(self):
        matched, _ = engine.match_conditions(
            {"vendor_chain_min_length": 3}, PROFILE)
        self.assertTrue(matched)
        matched, _ = engine.match_conditions(
            {"vendor_chain_min_length": 4}, PROFILE)
        self.assertFalse(matched)


class RuleApplicableTest(unittest.TestCase):
    def test_deployment_country_applies(self):
        rule = {"id": "R", "jurisdiction": "SG", "theme": "data_protection"}
        self.assertTrue(engine.rule_applicable(rule, PROFILE))

    def test_origin_country_only_for_cross_border_theme(self):
        xfer = {"id": "R1", "jurisdiction": "CN", "theme": "cross_border_transfer"}
        local = {"id": "R2", "jurisdiction": "CN", "theme": "minor_protection"}
        self.assertTrue(engine.rule_applicable(xfer, PROFILE))
        self.assertFalse(engine.rule_applicable(local, PROFILE))

    def test_unrelated_jurisdiction_skipped(self):
        rule = {"id": "R", "jurisdiction": "VN", "theme": "data_residency"}
        self.assertFalse(engine.rule_applicable(rule, PROFILE))


class EvaluateTest(unittest.TestCase):
    RULESET = {"rules": [
        {"id": "R-BLOCK", "jurisdiction": "ALL", "theme": "t", "severity": "block",
         "when": {"data_categories_any": ["personal"]},
         "obligations": ["ob.a"],
         "required_evidence": ["ev.missing"],
         "required_certs": ["cert.x"],
         "required_roles": ["dpo"]},
        {"id": "R-REVIEW", "jurisdiction": "ALL", "theme": "t", "severity": "review",
         "when": {"purposes_any": ["credit_scoring"]},
         "obligations": ["ob.b"], "required_evidence": ["ev.missing2"],
         "required_roles": ["legal"]},
        {"id": "R-SKIP", "jurisdiction": "VN", "theme": "t", "severity": "block",
         "when": {"data_categories_any": ["personal"]},
         "obligations": [], "required_roles": []},
    ]}

    def test_blocked_with_gaps_and_trace(self):
        result = engine.evaluate(PROFILE, self.RULESET, [], [], NOW)
        self.assertEqual(result["decision"], "BLOCKED")
        gap_ids = {g["id"] for g in result["evidence_gaps"]}
        self.assertEqual(gap_ids, {"ev.missing", "cert.x", "ev.missing2"})
        self.assertEqual(result["required_roles"], ["dpo", "legal"])
        self.assertEqual({o["id"] for o in result["obligations"]}, {"ob.a", "ob.b"})
        # 判定路径可复核：每条规则都有适用/命中记录
        trace = {t["rule_id"]: t for t in result["trace"]}
        self.assertFalse(trace["R-SKIP"]["applicable"])
        self.assertTrue(trace["R-BLOCK"]["matched"])
        self.assertTrue(trace["R-BLOCK"]["reasons"])

    def test_conditional_when_only_review_gaps(self):
        ruleset = {"rules": [r for r in self.RULESET["rules"]
                             if r["id"] == "R-REVIEW"]}
        result = engine.evaluate(PROFILE, ruleset, [], [], NOW)
        self.assertEqual(result["decision"], "CONDITIONAL")

    def test_approved_when_no_gaps(self):
        profile = {**PROFILE, "evidence": [
            {"id": "ev.missing"}, {"id": "ev.missing2"}]}
        certs = [{"cert_type": "cert.x", "expires_at": "2099-01-01T00:00:00+00:00"}]
        result = engine.evaluate(profile, self.RULESET, [], certs, NOW)
        self.assertEqual(result["decision"], "APPROVED")
        self.assertEqual(result["uncovered_gaps"], [])

    def test_expired_cert_is_gap(self):
        certs = [{"cert_type": "cert.x", "expires_at": "2020-01-01T00:00:00+00:00"}]
        result = engine.evaluate(PROFILE, self.RULESET, [], certs, NOW)
        kinds = {g["kind"] for g in result["evidence_gaps"]}
        self.assertIn("cert_expired", kinds)

    def test_exemption_covers_gap_within_scope_and_expiry(self):
        exemptions = [{"exemption_id": "exm_1", "rule_ids": ["R-BLOCK"],
                       "evidence_ids": [], "obligation_ids": [],
                       "expires_at": "2099-01-01T00:00:00+00:00"}]
        result = engine.evaluate(PROFILE, self.RULESET, exemptions, [], NOW)
        covered = {g["id"] for g in result["evidence_gaps"] if g["covered_by"]}
        self.assertEqual(covered, {"ev.missing", "cert.x"})
        self.assertEqual({g["id"] for g in result["uncovered_gaps"]}, {"ev.missing2"})

    def test_expired_exemption_does_not_cover(self):
        exemptions = [{"exemption_id": "exm_old", "rule_ids": ["R-BLOCK"],
                       "evidence_ids": [], "obligation_ids": [],
                       "expires_at": "2020-01-01T00:00:00+00:00"}]
        result = engine.evaluate(PROFILE, self.RULESET, exemptions, [], NOW)
        self.assertEqual(len(result["uncovered_gaps"]), 3)


if __name__ == "__main__":
    unittest.main()
