"""测试辅助：内存服务 + HTTP 请求客户端。"""
import json
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

from service.api import ApiHandler, build_app
from service.clock import FixedClock
from service.seed_rules import SEED_PACKAGE


class ApiServer:
    def __init__(self, seed=True, clock=None, db_path=":memory:"):
        self.app = build_app(db_path, with_worker=False, seed=seed,
                             clock=clock)
        ApiHandler.app = self.app
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ApiHandler)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def drain(self, timeout=5):
        self.app.worker.run_pending(timeout=timeout)

    def request(self, method, path, body=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = json.dumps(body) if body is not None else None
        headers = {"Content-Type": "application/json"} if payload else {}
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        data = json.loads(raw) if raw else None
        return resp.status, data

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, body):
        return self.request("POST", path, body)


# 一个典型的跨境未成年人高风险场景（部署在新加坡、数据主体含中国）
CROSS_BORDER_PROFILE = {
    "model_version": "vision-v3.2.1",
    "data_categories": ["personal_information", "biometric_data"],
    "purpose": "credit_scoring",
    "deployment_jurisdiction": "SG",
    "data_subject_jurisdictions": ["SG", "CN"],
    "vendor_chain": [
        {"name": "CloudHostSG", "jurisdiction": "SG"},
        {"name": "LabelCoID", "jurisdiction": "ID"},
    ],
    "involves_minors": True,
    "high_risk_decision": True,
}
