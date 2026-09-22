"""基础服务测试。"""
import unittest

from tests.support import ApiServer


class HealthTest(unittest.TestCase):
    def test_health(self):
        server = ApiServer(seed=False)
        try:
            status, body = server.get("/health")
            self.assertEqual(status, 200)
            self.assertEqual(body, {"status": "ok"})
        finally:
            server.stop()


if __name__ == "__main__":
    unittest.main()
