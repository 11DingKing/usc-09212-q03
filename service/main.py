"""项目服务入口：跨境算法合规判定 API。"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re

from .core import ComplianceService, DomainError
from .seed import seed_if_empty
from .store import Store
from .worker import QueueWorker

DEFAULT_DB_PATH = os.environ.get("SERVICE_DB_PATH", "./data/compliance.db")


def make_handler(service: ComplianceService):
    """构建绑定指定服务实例的请求处理器。"""

    def get_ruleset(ruleset_id):
        ruleset = service.store.get_ruleset(ruleset_id)
        if ruleset is None:
            raise DomainError("not_found", f"规则集不存在: {ruleset_id}", 404)
        return ruleset

    def post_release(body):
        decision = service.request_release(body["profile_id"],
                                           body.get("profile_version"))
        status = 202 if decision.get("status") == "EVALUATION_PENDING" else 201
        return status, decision

    routes = [
        ("GET", r"^/health$", lambda m, b: (200, {"status": "ok"})),
        ("PUT", r"^/v1/profiles/(?P<profile_id>[^/]+)$",
         lambda m, b: (201, service.put_profile(m["profile_id"], b))),
        ("GET", r"^/v1/profiles/(?P<profile_id>[^/]+)$",
         lambda m, b: (200, service.get_profile(m["profile_id"]))),
        ("GET", r"^/v1/profiles/(?P<profile_id>[^/]+)/versions/(?P<version>\d+)$",
         lambda m, b: (200, service.get_profile(m["profile_id"],
                                                int(m["version"])))),
        ("POST", r"^/v1/rulesets$",
         lambda m, b: (201, service.register_ruleset(b["ruleset_id"], b["rules"],
                                                     actor=b.get("actor")))),
        ("GET", r"^/v1/rulesets/(?P<ruleset_id>[^/]+)/latest$",
         lambda m, b: (200, get_ruleset(m["ruleset_id"]))),
        ("POST", r"^/v1/reviewers$",
         lambda m, b: (201, service.register_reviewer(
             b["reviewer_id"], b["org"], b["roles"], b.get("conflicts")))),
        ("POST", r"^/v1/evaluations$",
         lambda m, b: (202, {"eval_id": service.request_evaluation(
             b["profile_id"], b.get("profile_version"),
             ruleset_id=b.get("ruleset_id"))})),
        ("GET", r"^/v1/evaluations/(?P<eval_id>[^/]+)$",
         lambda m, b: (200, service.get_evaluation(m["eval_id"]))),
        ("POST", r"^/v1/evaluations/(?P<eval_id>[^/]+)/signatures$",
         lambda m, b: (201, service.sign(m["eval_id"], b["reviewer_id"],
                                         b["role"]))),
        ("POST", r"^/v1/exemptions$",
         lambda m, b: (201, service.grant_exemption(
             b["profile_id"], b["expires_at"], b["reason"], b["granted_by"],
             rule_ids=b.get("rule_ids"), evidence_ids=b.get("evidence_ids"),
             obligation_ids=b.get("obligation_ids")))),
        ("POST", r"^/v1/certificates$",
         lambda m, b: (201, service.register_certificate(
             b["profile_id"], b["cert_type"], b["issuer"], b["expires_at"]))),
        ("POST", r"^/v1/releases$", lambda m, b: post_release(b)),
        ("GET", r"^/v1/releases/(?P<release_id>[^/]+)$",
         lambda m, b: (200, service.get_release(m["release_id"]))),
        ("POST", r"^/v1/traffic/authorize$",
         lambda m, b: (200, service.authorize_traffic(b["release_id"]))),
        ("GET", r"^/v1/queue$", lambda m, b: (200, service.store.queue_counts())),
        ("GET", r"^/v1/audit$",
         lambda m, b: (200, {"entries": service.store.list_audit()})),
    ]
    compiled = [(method, re.compile(pattern), fn) for method, pattern, fn in routes]

    class Handler(BaseHTTPRequestHandler):
        """合规判定 REST 接口。"""

        def _handle(self, method):
            body = {}
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                try:
                    body = json.loads(self.rfile.read(length))
                except json.JSONDecodeError:
                    self._reply(400, {"error": {"code": "bad_json",
                                                "message": "请求体不是合法 JSON"}})
                    return
            for route_method, pattern, fn in compiled:
                if route_method != method:
                    continue
                match = pattern.match(self.path)
                if not match:
                    continue
                try:
                    status, payload = fn(match.groupdict(), body)
                except DomainError as exc:
                    self._reply(exc.status, {"error": {"code": exc.code,
                                                       "message": str(exc)}})
                except (KeyError, TypeError) as exc:
                    self._reply(400, {"error": {"code": "bad_request",
                                                "message": f"缺少参数: {exc}"}})
                else:
                    self._reply(status, payload)
                return
            self._reply(404, {"error": {"code": "not_found",
                                        "message": "路径不存在"}})

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def do_PUT(self):
            self._handle("PUT")

        def _reply(self, status, payload):
            data = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format, *args):
            return

    return Handler


def build_service(db_path: str = DEFAULT_DB_PATH):
    """装配服务：打开存储、写入基线规则、恢复并启动评估队列。"""
    if db_path != ":memory:":
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    store = Store(db_path)
    seed_if_empty(store)
    service = ComplianceService(store)
    worker = QueueWorker(service)
    worker.start()
    return service, worker


def run():
    """启动本地服务。"""
    service, worker = build_service()
    handler = make_handler(service)
    server = ThreadingHTTPServer(("127.0.0.1", 8000), handler)
    try:
        server.serve_forever()
    finally:
        worker.stop()
        service.store.close()


if __name__ == "__main__":
    run()
