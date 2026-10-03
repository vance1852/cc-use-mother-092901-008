"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, ValidationError
from .service import DomainService
from .storage import Database
from .supply import SupplyService


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    segments = [segment for segment in parsed.path.split("/") if segment]
    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and parsed.path == "/organizations":
            receipt = service.register_organization(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/actors":
            receipt = service.register_actor(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/sites":
            receipt = service.register_site(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/domain-records":
            receipt = service.record_domain_data(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/domain-records":
            query = parse_qs(parsed.query)
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = query.get("category", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            query = parse_qs(parsed.query)
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}
        result = _supply_route(service, method, segments, parsed.query, body, actor_id)
        if result is not None:
            return result
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


def _supply_route(service, method: str, segments: list[str], query: str,
                  body: dict[str, Any], actor_id: str):
    """保供运力相关路由；返回 None 表示未命中。"""

    supply = getattr(service, "supply", None)
    if supply is None:
        return None
    query_pairs = parse_qs(query)
    receipt_targets = {
        ("POST", ("corridors",)): supply.register_corridor,
        ("POST", ("customer-groups",)): supply.register_group,
        ("POST", ("customers",)): supply.register_customer,
        ("POST", ("policy-versions",)): supply.register_policy_version,
        ("POST", ("train-versions",)): supply.register_train_version,
        ("POST", ("demands",)): supply.submit_demand,
        ("POST", ("exceptions",)): supply.propose_exception,
    }
    for (http_method, path_segments), handler in receipt_targets.items():
        if method == http_method and tuple(segments) == path_segments:
            receipt = handler(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__

    if method == "POST" and len(segments) == 3 and segments[0] == "demands":
        demand_id = segments[1]
        if segments[2] == "confirm":
            receipt = supply.confirm_demand(actor_id=actor_id, demand_id=demand_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if segments[2] == "waive":
            receipt = supply.waive_demand(actor_id=actor_id, demand_id=demand_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if segments[2] == "shipment":
            receipt = supply.record_shipment(actor_id=actor_id, demand_id=demand_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
    if method == "POST" and len(segments) == 3 and segments[0] == "train-versions":
        version_id = segments[1]
        if segments[2] == "freeze":
            receipt = supply.freeze_demand_book(actor_id=actor_id, version_id=version_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if segments[2] == "capacity":
            receipt = supply.adjust_capacity(actor_id=actor_id, version_id=version_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if segments[2] == "cancel":
            receipt = supply.cancel_train(actor_id=actor_id, version_id=version_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
    if method == "POST" and len(segments) == 3 and segments[0] == "exceptions" and segments[2] == "decision":
        receipt = supply.decide_exception(actor_id=actor_id, exception_id=segments[1], **body)
        return 200 if receipt.replayed else 201, receipt.__dict__
    if method == "GET" and len(segments) == 2 and segments[0] == "demands":
        return 200, supply.get_demand_view(segments[1])
    if method == "GET" and tuple(segments) == ("runs",):
        version_id = query_pairs.get("version_id", [""])[0]
        if not version_id:
            raise ValidationError("version_id 不能为空")
        return 200, {"items": supply.list_runs(version_id)}
    if method == "GET" and len(segments) == 2 and segments[0] == "runs":
        return 200, supply.get_run(segments[1])
    if method == "GET" and len(segments) == 3 and segments[0] == "runs" and segments[2] == "recompute":
        return 200, supply.recompute_run(segments[1])
    if method == "GET" and tuple(segments) == ("policy-comparison",):
        version_id = query_pairs.get("version_id", [""])[0]
        ids = query_pairs.get("policy_version_ids", [""])[0].split(",")
        ids = [item.strip() for item in ids if item.strip()]
        return 200, supply.compare_policies(version_id=version_id, policy_version_ids=ids)
    return None


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动技能赛训协作基础服务")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    domain_service = DomainService(database)
    domain_service.supply = SupplyService(database)
    Handler.service = domain_service
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
