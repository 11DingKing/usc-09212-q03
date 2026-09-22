"""HTTP API：发布档案、规则包、证据、证明、豁免、签署与判定接口。"""
import json
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from .errors import ApiError, NotFoundError
from .storage import Store
from .clock import Clock
from .engine import App
from .seed_rules import SEED_PACKAGE


def build_app(path=":memory:", with_worker=False, seed=False, clock=None):
    store = Store(path)
    app = App(store, clock or Clock())
    if seed:
        package, _ = app.rules.publish(SEED_PACKAGE)
    if with_worker:
        app.start_worker()
    return app


class ApiHandler(BaseHTTPRequestHandler):
    app = None  # 由 run() / 测试注入到类属性

    # -- HTTP 基础 ------------------------------------------------------

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def log_message(self, format, *args):
        return

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        try:
            body = self._read_body() if method == "POST" else {}
            handler = self._router(method, path)
            if handler is None:
                self._send_json(404, {"error": {"code": "not_found",
                                                "message": "未知路径"}})
                return
            status, payload = handler(body, query)
            self._send_json(status, payload)
        except ApiError as exc:
            self._send_json(exc.status,
                            {"error": {"code": exc.code,
                                       "message": str(exc)}})
        except Exception as exc:  # 防御性兜底，避免连接挂死
            self._send_json(500, {"error": {"code": "internal_error",
                                            "message": str(exc)}})

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        if length > 1_000_000:
            raise ApiError("请求体过大", status=413, code="payload_too_large")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise ApiError("请求体不是合法 JSON")
        if not isinstance(data, dict):
            raise ApiError("请求体必须是 JSON 对象")
        return data

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- 路由表 ---------------------------------------------------------

    def _router(self, method, path):
        app = self.app
        segments = [s for s in path.split("/") if s]
        routes = {
            ("GET", "/health"): lambda b, q: (200, {"status": "ok"}),
            ("POST", "/releases"): self.create_release,
            ("GET", "/releases"): lambda b, q: (
                200, {"releases": app.releases.list_releases()}),
            ("POST", "/rules"): self.publish_rules,
            ("GET", "/rules"): lambda b, q: (
                200, {"packages": app.rules.list_packages(),
                      "active_version": app.rules.active_version()}),
            ("POST", "/evidence"): self.add_evidence,
            ("POST", "/certificates"): self.register_certificate,
            ("GET", "/certificates"): lambda b, q: (
                200, {"certificates": app.certificates.list_all()}),
            ("POST", "/waivers"): self.grant_waiver,
            ("GET", "/waivers"): lambda b, q: (
                200, {"waivers": app.waivers.list_all()}),
            ("POST", "/reviewers"): self.upsert_reviewer,
            ("GET", "/queue"): lambda b, q: (
                200, {"jobs": app.queue.list_jobs()}),
            ("POST", "/queue/drain"): self.drain_queue,
            ("GET", "/audit/chain"): self.audit_chain,
            ("POST", "/evaluations"): self.enqueue_evaluation,
        }
        handler = routes.get((method, path))
        if handler:
            return handler

        if len(segments) == 2 and segments[0] == "releases":
            rid = segments[1]
            if method == "GET":
                return lambda b, q: (200, app.releases.get_release(rid))
        if len(segments) == 3 and segments[0] == "releases" \
                and segments[2] == "revisions" and method == "POST":
            rid = segments[1]
            return lambda b, q: self.amend_release(rid, b)
        if len(segments) == 3 and segments[0] == "releases" \
                and segments[2] == "evidence" and method == "GET":
            rid = segments[1]
            return lambda b, q: (
                200, {"evidence": app.evidence.list_for_release(rid)})
        if len(segments) == 3 and segments[0] == "releases" \
                and segments[2] == "decision" and method == "GET":
            rid = segments[1]
            return lambda b, q: (200, app.gate.decision(rid))
        if len(segments) == 3 and segments[0] == "releases" \
                and segments[2] == "signoffs":
            rid = segments[1]
            if method == "POST":
                return lambda b, q: self.create_signoff(rid, b)
            if method == "GET":
                if "assessment_id" not in q:
                    raise ApiError("query 参数 assessment_id 必填")
                assessment_id = int(q["assessment_id"][0])
                return lambda b, q: (
                    200, {"signoffs": app.signoffs.list_for(
                        rid, assessment_id)})
        if len(segments) == 3 and segments[0] == "releases" \
                and segments[2] == "assessments" and method == "GET":
            rid = segments[1]
            return lambda b, q: (200, self.list_assessments(rid))
        if len(segments) == 2 and segments[0] == "assessments" \
                and method == "GET":
            return lambda b, q: (200, self.get_assessment(int(segments[1])))
        if len(segments) == 2 and segments[0] == "reviewers" \
                and method == "GET":
            return lambda b, q: (200, app.reviewers.get(segments[1]))
        if len(segments) == 3 and segments[0] == "certificates" \
                and segments[2] == "revoke" and method == "POST":
            return lambda b, q: (200, app.certificates.revoke(segments[1]))
        if len(segments) == 3 and segments[0] == "waivers" \
                and segments[2] == "revoke" and method == "POST":
            return lambda b, q: (200, app.waivers.revoke(segments[1]))
        if len(segments) == 2 and segments[0] == "rules" and method == "GET":
            return lambda b, q: (200, app.rules.get_package(segments[1]))
        return None

    # -- 端点处理 -------------------------------------------------------

    def create_release(self, body, query):
        rid = body.pop("id", None)
        if not rid:
            raise ApiError("id 必填")
        release = self.app.releases.register(rid, body)
        job_id = self.app.enqueue_after_register(rid, "release_registered")
        return 201, {"release": release, "enqueued_job_id": job_id}

    def amend_release(self, rid, body):
        release = self.app.releases.amend(rid, body)
        job_id = self.app.enqueue_after_register(rid, "profile_amended")
        return 201, {"release": release, "enqueued_job_id": job_id}

    def publish_rules(self, body, query):
        package, created = self.app.rules.publish(body)
        affected = self.app.enqueue_affected_by_rules(package)
        return (201 if created else 200), {
            "package": {"version": package["version"],
                        "published_at": package["published_at"],
                        "content_hash": package["content_hash"]},
            "created": created,
            "affected_releases": affected,
        }

    def add_evidence(self, body, query):
        required = ["id", "release_id", "kind"]
        for key in required:
            if not body.get(key):
                raise ApiError(f"{key} 必填")
        evidence = self.app.evidence.add(
            body["release_id"], body["id"], body["kind"],
            body.get("description", ""), body.get("valid_until"))
        return 201, {"evidence": evidence}

    def register_certificate(self, body, query):
        for key in ["id", "issuer", "scope", "valid_from", "valid_until"]:
            if not body.get(key):
                raise ApiError(f"{key} 必填")
        cert = self.app.certificates.register(
            body["id"], body["issuer"], body["scope"],
            body["valid_from"], body["valid_until"],
            body.get("release_id"), body.get("jurisdiction"))
        # 证明登记后自动安排相关发布复评
        for release in self.app.releases.list_releases():
            self.app.queue.enqueue_for_current(
                release["id"], "certificate_registered", self.app.rules)
        return 201, {"certificate": cert}

    def grant_waiver(self, body, query):
        for key in ["id", "release_id", "jurisdiction", "obligation_id",
                    "scope_note", "valid_from", "valid_until"]:
            if not body.get(key):
                raise ApiError(f"{key} 必填")
        waiver = self.app.waivers.grant(
            body["id"], body["release_id"], body["jurisdiction"],
            body["obligation_id"], body["scope_note"],
            body["valid_from"], body["valid_until"])
        self.app.queue.enqueue_for_current(
            body["release_id"], "waiver_granted", self.app.rules)
        return 201, {"waiver": waiver}

    def upsert_reviewer(self, body, query):
        if not body.get("reviewer_id"):
            raise ApiError("reviewer_id 必填")
        reviewer = self.app.reviewers.upsert(
            body["reviewer_id"], body.get("affiliations", []))
        return 200, {"reviewer": reviewer}

    def create_signoff(self, rid, body):
        for key in ["assessment_id", "role", "reviewer_id", "decision"]:
            if body.get(key) is None:
                raise ApiError(f"{key} 必填")
        signoff = self.app.signoffs.sign(
            rid, int(body["assessment_id"]), body["role"],
            body["reviewer_id"], body["decision"], body.get("comment"))
        return 201, {"signoff": signoff}

    def enqueue_evaluation(self, body, query):
        rid = body.get("release_id")
        if not rid:
            raise ApiError("release_id 必填")
        revision = body.get("revision")
        if revision is None:
            revision = self.app.releases.get_release(rid)["latest_revision"]
        rule_version = body.get("rule_version") or \
            self.app.rules.active_version()
        if not rule_version:
            raise ApiError("尚无已发布规则包")
        reason = body.get("reason", "manual")
        job_id = self.app.queue.enqueue(rid, int(revision), rule_version,
                                        reason)
        return 202, {"job_id": job_id}

    def drain_queue(self, body, query):
        timeout = float(body.get("timeout", 10))
        self.app.worker.run_pending(timeout=timeout)
        return 200, {"jobs": self.app.queue.list_jobs()}

    def audit_chain(self, body, query):
        count, ok = self.app.store.verify_chain()
        return 200, {"assessment_count": count, "chain_intact": ok,
                     "chain_tip": self.app.store.last_assessment_hash()}

    def list_assessments(self, rid):
        self.app.releases._require(rid)
        rows = self.app.store.query(
            "SELECT id, revision, rule_version, evaluated_at, decision, "
            "entry_hash FROM assessments WHERE release_id=? ORDER BY id",
            (rid,))
        return {"assessments": [dict(r) for r in rows]}

    def get_assessment(self, assessment_id):
        row = self.app.store.query_one(
            "SELECT * FROM assessments WHERE id=?", (assessment_id,))
        if not row:
            raise NotFoundError("判定记录不存在")
        return {
            "id": row["id"],
            "release_id": row["release_id"],
            "revision": row["revision"],
            "rule_version": row["rule_version"],
            "evaluated_at": row["evaluated_at"],
            "decision": row["decision"],
            "triggered_obligations": json.loads(row["triggered_obligations"]),
            "evidence_gaps": json.loads(row["evidence_gaps"]),
            "missing_roles": json.loads(row["missing_roles"]),
            "rationale": json.loads(row["rationale"]),
            "evidence_ids": json.loads(row["evidence_ids"]),
            "cert_snapshots": json.loads(row["cert_snapshots"]),
            "input_refs": json.loads(row["input_refs"]),
            "prev_hash": row["prev_hash"],
            "entry_hash": row["entry_hash"],
        }
