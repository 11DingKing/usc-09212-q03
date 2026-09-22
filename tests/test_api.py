"""HTTP 接口端到端测试：从建档到发布放行。"""
import json
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

from service.core import ComplianceService
from service.main import make_handler
from service.seed import seed_if_empty
from service.store import Store
from service.worker import QueueWorker

FUTURE = "2099-01-01T00:00:00+00:00"

PROFILE = {
    "model_version": "m-1.0.0",
    "owner_org": "OrgA",
    "data_categories": ["personal"],
    "purposes": ["chat"],
    "data_origin_country": "CN",
    "deployment_country": "SG",
    "vendor_chain": [{"org": "VendorCloud", "role": "hosting"}],
    "evidence": [
        {"id": "ev.cn.security_assessment_report"},
        {"id": "ev.sg.consent_records"},
        {"id": "ev.sg.transfer_agreement"},
    ],
}


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        store = Store(":memory:")
        seed_if_empty(store)
        cls.service = ComplianceService(store)
        cls.worker = QueueWorker(cls.service)
        cls.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(cls.service))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def call(self, method, path, body=None):
        client = HTTPConnection("127.0.0.1", self.server.server_port)
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        client.request(method, path, body=data, headers=headers)
        response = client.getresponse()
        payload = json.loads(response.read())
        client.close()
        return response.status, payload

    def test_full_flow_over_http(self):
        status, profile = self.call("PUT", "/v1/profiles/p-http", PROFILE)
        self.assertEqual(status, 201)
        self.assertEqual(profile["version"], 1)

        status, _ = self.call("POST", "/v1/certificates", {
            "profile_id": "p-http", "cert_type": "cert.cac_security_assessment",
            "issuer": "CAC", "expires_at": FUTURE})
        self.assertEqual(status, 201)

        status, eval_resp = self.call("POST", "/v1/evaluations",
                                      {"profile_id": "p-http"})
        self.assertEqual(status, 202)
        eval_id = eval_resp["eval_id"]
        self.worker.run_until_idle()

        status, evaluation = self.call("GET", f"/v1/evaluations/{eval_id}")
        self.assertEqual(status, 200)
        self.assertEqual(evaluation["result"]["decision"], "APPROVED")

        for reviewer_id, role in (("rev-dpo", "dpo"), ("rev-legal", "legal")):
            self.call("POST", "/v1/reviewers", {
                "reviewer_id": reviewer_id, "org": "ReviewOrg", "roles": [role]})
            status, _ = self.call(
                "POST", f"/v1/evaluations/{eval_id}/signatures",
                {"reviewer_id": reviewer_id, "role": role})
            self.assertEqual(status, 201)

        # 利益冲突的签署被隔离
        self.call("POST", "/v1/reviewers", {
            "reviewer_id": "rev-vendor", "org": "VendorCloud", "roles": ["dpo"]})
        status, error = self.call(
            "POST", f"/v1/evaluations/{eval_id}/signatures",
            {"reviewer_id": "rev-vendor", "role": "dpo"})
        self.assertEqual(status, 409)

        status, release = self.call("POST", "/v1/releases",
                                    {"profile_id": "p-http"})
        self.assertEqual(status, 201)
        self.assertEqual(release["status"], "ACTIVE")
        # 发布接口返回具体义务、证据缺口与可复核判定路径
        self.assertIn("obligations", release)
        self.assertIn("evidence_gaps", release)
        self.assertTrue(release["trace"])

        status, traffic = self.call("POST", "/v1/traffic/authorize",
                                    {"release_id": release["release_id"]})
        self.assertEqual(status, 200)
        self.assertTrue(traffic["allowed"])

        status, queue = self.call("GET", "/v1/queue")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(queue["DONE"], 1)

    def test_unknown_route_404(self):
        status, payload = self.call("GET", "/nope")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
