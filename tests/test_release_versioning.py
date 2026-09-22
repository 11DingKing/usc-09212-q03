"""发布档案：版本化修订、校验与哈希指纹。"""
import unittest

from service.api import build_app
from service.errors import ConflictError, ValidationError


PROFILE = {
    "model_version": "reco-v1.0",
    "data_categories": ["personal_information"],
    "purpose": "content_recommendation",
    "deployment_jurisdiction": "cn",
    "vendor_chain": [{"name": "Acme", "jurisdiction": "sg"}],
    "data_subject_jurisdictions": ["cn"],
    "involves_minors": False,
    "high_risk_decision": False,
}


class ReleaseVersioningTest(unittest.TestCase):
    def setUp(self):
        self.app = build_app()

    def test_register_creates_revision_one(self):
        release = self.app.releases.register("rel-1", PROFILE)
        self.assertEqual(release["latest_revision"], 1)
        self.assertEqual(len(release["revisions"]), 1)
        # 辖区代码统一规范化为大写
        self.assertEqual(release["profile"]["deployment_jurisdiction"], "CN")

    def test_duplicate_id_conflicts(self):
        self.app.releases.register("rel-1", PROFILE)
        with self.assertRaises(ConflictError):
            self.app.releases.register("rel-1", PROFILE)

    def test_amend_appends_revision_without_overwriting(self):
        self.app.releases.register("rel-1", PROFILE)
        amended = dict(PROFILE, model_version="reco-v1.1")
        release = self.app.releases.amend("rel-1", amended)
        self.assertEqual(release["latest_revision"], 2)
        # 旧修订原样保留
        old, _ = self.app.releases.get_profile("rel-1", 1)
        self.assertEqual(old["model_version"], "reco-v1.0")
        current, rev = self.app.releases.get_profile("rel-1")
        self.assertEqual(rev, 2)
        self.assertEqual(current["model_version"], "reco-v1.1")
        self.assertEqual(len(release["revisions"]), 2)

    def test_distinct_profiles_have_distinct_hashes(self):
        r1 = self.app.releases.register("rel-1", PROFILE)
        self.app.releases.amend("rel-1", dict(PROFILE, purpose="generative_ai"))
        r2 = self.app.releases.get_release("rel-1")
        self.assertNotEqual(r1["profile_hash"], r2["profile_hash"])

    def test_validation(self):
        for bad in [
            dict(PROFILE, model_version=""),
            dict(PROFILE, data_categories=[]),
            dict(PROFILE, purpose=""),
            dict(PROFILE, deployment_jurisdiction=""),
            dict(PROFILE, vendor_chain=[{"name": "x"}]),
        ]:
            with self.assertRaises(ValidationError):
                self.app.releases.register("x", bad)


if __name__ == "__main__":
    unittest.main()
