"""市场监管低价竞争办案服务的 HTTP 接口（仅标准库）。

运行：python -m src.api --port 8080 [--data store.json]
调用方通过 X-User 请求头表明身份；--data 指定快照文件时每次写操作后自动落盘。
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .service import CaseError, CaseService, NotFoundError, StateError


def _make_handler(service: CaseService, data_path: str | None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "CaseService/0.1"

        # ---- 基础工具 ----

        def _send(self, code: int, body) -> None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def _user(self) -> str:
            # 中文姓名需按百分号编码的 UTF-8 传送（HTTP 头仅支持 latin-1）
            return unquote(self.headers.get("X-User", "匿名"))

        def _query(self, name: str):
            values = parse_qs(urlparse(self.path).query).get(name)
            return values[0] if values else None

        def log_message(self, fmt, *args):  # 保持测试输出安静
            pass

        # ---- 路由 ----

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def _dispatch(self, method: str) -> None:
            try:
                code, body = self._route(method)
                if method == "POST" and data_path:
                    service.save(data_path)
            except NotFoundError as exc:
                code, body = 404, {"error": str(exc)}
            except StateError as exc:
                code, body = 409, {"error": str(exc)}
            except (CaseError, ValueError, KeyError) as exc:
                code, body = 400, {"error": str(exc)}
            self._send(code, body)

        def _route(self, method: str):
            segments = [s for s in urlparse(self.path).path.split("/") if s]
            body = self._body() if method == "POST" else {}
            user = self._user()

            if segments == ["cases"] and method == "POST":
                case = service.register_clue(body["case_id"], body["title"], body.get("source", ""), body.get("handler", user))
                return 201, service.overview(case.case_id)

            if len(segments) >= 2 and segments[0] == "cases":
                case_id, rest = segments[1], segments[2:]

                if not rest and method == "GET":
                    return 200, service.overview(case_id)
                if rest == ["open"] and method == "POST":
                    service.open_investigation(
                        case_id,
                        product_specs=body["product_specs"],
                        channels=body["channels"],
                        window_start=body["window_start"],
                        window_end=body["window_end"],
                        cost_standard_id=body["cost_standard_id"],
                        author=user,
                    )
                    return 200, service.overview(case_id)
                if rest == ["dismiss"] and method == "POST":
                    service.dismiss(case_id, user, body["reason"])
                    return 200, service.overview(case_id)
                if rest == ["records"] and method == "POST":
                    record = service.append_record(case_id, body["kind"], body["payload"], user, bool(body.get("secret")))
                    return 201, {"seq": record.seq, "hash": record.hash}
                if rest == ["records"] and method == "GET":
                    version = self._query("version")
                    return 200, service.read_records(case_id, user, int(version) if version else None)
                if rest == ["scope"] and method == "GET":
                    return 200, service.gap_report(case_id)
                if rest == ["assessment"] and method == "GET":
                    version = self._query("version")
                    result = service.assess(case_id, int(version) if version else None)
                    return 200, result.to_payload()
                if rest == ["notice"] and method == "POST":
                    service.notify(case_id, user, body["proposed"], int(body.get("appeal_days", 5)))
                    return 200, service.overview(case_id)
                if rest == ["appeals"] and method == "POST":
                    record = service.appeal(case_id, user, body["content"])
                    return 201, {"seq": record.seq, "hash": record.hash}
                if rest == ["decision"] and method == "POST":
                    record = service.decide(case_id, user, body["penalty"])
                    return 201, {"seq": record.seq, "hash": record.hash}
                if rest == ["reproduce"] and method == "GET":
                    version = self._query("version")
                    return 200, service.reproduce(case_id, user, int(version) if version else None)
                if rest == ["grants"] and method == "POST":
                    grant = service.grant_secret_access(case_id, body["grantee"], user, int(body.get("days", 7)))
                    return 201, {"grantee": grant.grantee, "expires_at": grant.expires_at.isoformat()}
                if rest == ["rectifications"] and method == "POST":
                    service.submit_rectification(case_id, user, body["report"])
                    return 200, service.overview(case_id)
                if rest == ["verify"] and method == "POST":
                    return 200, service.verify_rectification(case_id, user, body.get("window_end"))
                if rest == ["tasks"] and method == "GET":
                    return 200, service.list_tasks(case_id)
                if len(rest) == 3 and rest[0] == "tasks" and rest[2] == "complete" and method == "POST":
                    return 200, service.complete_task(case_id, rest[1], user)

            return 404, {"error": "接口不存在"}

    return Handler


def run(port: int = 8080, data_path: str | None = None) -> None:
    service = CaseService()
    if data_path and Path(data_path).exists():
        service = CaseService.load(data_path)
    server = ThreadingHTTPServer(("0.0.0.0", port), _make_handler(service, data_path))
    print(f"办案服务已启动：http://127.0.0.1:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if data_path:
            service.save(data_path)
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="市场监管低价竞争办案服务")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--data", help="快照文件路径，指定后启用落盘与恢复")
    args = parser.parse_args()
    run(args.port, args.data)


if __name__ == "__main__":
    main()
