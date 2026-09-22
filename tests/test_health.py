"""基础服务测试。"""
import json
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

from service.core import ComplianceService
from service.main import make_handler
from service.store import Store


def make_server():
    handler = make_handler(ComplianceService(Store(":memory:")))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


class HealthTest(unittest.TestCase):
    def test_health(self):
        server = make_server()
        try:
            client = HTTPConnection("127.0.0.1", server.server_port)
            client.request("GET", "/health")
            response = client.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read()), {"status": "ok"})
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
