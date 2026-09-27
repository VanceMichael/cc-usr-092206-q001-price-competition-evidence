import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from urllib.parse import quote

from src.api import _make_handler
from src.service import CaseService


def request(port, method, path, body=None, user="测试员"):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, method=method,
        headers={"Content-Type": "application/json", "X-User": quote(user)},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(CaseService(), None))
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_case_flow_over_http(self):
        status, case = request(self.port, "POST", "/cases", {
            "case_id": "API-001", "title": "接口测试案件", "source": "监测", "handler": "承办人甲",
        })
        self.assertEqual(status, 201)
        self.assertEqual(case["status"], "线索登记")

        status, case = request(self.port, "POST", "/cases/API-001/open", {
            "product_specs": ["SP-1"], "channels": ["线上-P", "线下-S"],
            "window_start": "2026-06-01", "window_end": "2026-06-30",
            "cost_standard_id": "CB-Q2",
        })
        self.assertEqual(status, 200)
        self.assertEqual(case["status"], "立案调查")

        for kind, payload in [
            ("商品规格", {"spec_id": "SP-1", "name": "样品"}),
            ("成本标准", {"standard_id": "CB-Q2", "items": [{"product_spec": "SP-1", "unit_cost": 800}],
                        "valid_from": "2026-04-01", "valid_to": "2026-06-30"}),
            ("线上交易证据", {"product_spec": "SP-1", "channel": "线上-P", "price": 500,
                          "quantity": 10, "sold_at": "2026-06-18", "order_id": "E-1"}),
            ("线下交易证据", {"product_spec": "SP-1", "channel": "线下-S", "price": 850,
                          "quantity": 5, "sold_at": "2026-06-20", "order_id": "L-1"}),
        ]:
            status, _ = request(self.port, "POST", "/cases/API-001/records", {"kind": kind, "payload": payload})
            self.assertEqual(status, 201)

        status, scope = request(self.port, "GET", "/cases/API-001/scope")
        self.assertEqual(status, 200)
        self.assertEqual(scope["gaps"], [])

        status, result = request(self.port, "GET", "/cases/API-001/assessment")
        self.assertEqual(status, 200)
        self.assertEqual(result["verdict"], "恶意倾销")

        status, _ = request(self.port, "POST", "/cases/API-001/notice",
                            {"proposed": {"拟处罚": "罚款"}, "appeal_days": 1})
        self.assertEqual(status, 200)
        status, _ = request(self.port, "POST", "/cases/API-001/appeals", {"content": "系清仓"}, user="经营者")
        self.assertEqual(status, 201)
        # 申辩期未满，决定应被拒绝
        status, body = request(self.port, "POST", "/cases/API-001/decision", {"penalty": {}})
        self.assertEqual(status, 409)
        self.assertIn("申辩期限未满", body["error"])

    def test_unknown_case_returns_404(self):
        status, body = request(self.port, "GET", "/cases/NOPE")
        self.assertEqual(status, 404)

    def test_bad_payload_returns_400(self):
        request(self.port, "POST", "/cases", {"case_id": "API-002", "title": "t", "source": "s", "handler": "h"})
        request(self.port, "POST", "/cases/API-002/open", {
            "product_specs": ["SP-1"], "channels": ["线上-P"],
            "window_start": "2026-06-01", "window_end": "2026-06-30", "cost_standard_id": "CB-Q2",
        })
        status, body = request(self.port, "POST", "/cases/API-002/records",
                               {"kind": "线上交易证据", "payload": {"price": 1}})
        self.assertEqual(status, 400)
        self.assertIn("缺少必填字段", body["error"])


if __name__ == "__main__":
    unittest.main()
