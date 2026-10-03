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
from .supply_service import SupplyService


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    segments = [segment for segment in parsed.path.split("/") if segment]
    query = parse_qs(parsed.query)
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
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = query.get("category", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}

        receipt_or_data = _supply_route(service, method, segments, query, body, actor_id)
        if receipt_or_data is not None:
            status_code, payload = receipt_or_data
            if "receipt" in payload:
                receipt = payload.pop("receipt")
                payload.update(receipt.__dict__)
                return 200 if receipt.replayed else status_code, payload
            return status_code, payload
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


def _supply_route(service, method: str, segments: list[str], query: dict[str, list[str]],
                  body: dict[str, Any], actor_id: str):
    """保供运力分配相关路由。返回 None 表示路径不匹配。"""

    if not isinstance(service, SupplyService):
        return None

    def receipt_response(status: int, receipt, **extra):
        return status, {"receipt": receipt, **extra}

    if method == "POST" and segments == ["corridors"]:
        return receipt_response(201, service.register_corridor(actor_id=actor_id, **body))
    if method == "POST" and segments == ["policies"]:
        return receipt_response(201, service.register_policy(actor_id=actor_id, **body))
    if method == "POST" and segments == ["policy-activations"]:
        return receipt_response(200, service.activate_policy(actor_id=actor_id, **body))
    if method == "POST" and segments == ["customers"]:
        return receipt_response(201, service.register_customer(actor_id=actor_id, **body))
    if method == "POST" and segments == ["departures"]:
        return receipt_response(201, service.register_departure(actor_id=actor_id, **body))
    if method == "POST" and segments == ["demands"]:
        return receipt_response(201, service.submit_demand(actor_id=actor_id, **body))
    if method == "POST" and segments == ["exceptions"]:
        return receipt_response(201, service.request_exception(actor_id=actor_id, **body))

    if len(segments) == 3 and segments[0] == "departures":
        departure_id = segments[1]
        if method == "POST" and segments[2] == "freeze":
            return receipt_response(200, service.freeze_and_allocate(
                actor_id=actor_id, departure_id=departure_id,
                request_id=body.get("request_id", "")))
        if method == "POST" and segments[2] == "capacity":
            return receipt_response(200, service.adjust_capacity(
                actor_id=actor_id, departure_id=departure_id, **body))
        if method == "POST" and segments[2] == "cancel":
            return receipt_response(200, service.cancel_departure(
                actor_id=actor_id, departure_id=departure_id, reason=body.get("reason", "")))
        if method == "POST" and segments[2] == "depart":
            return receipt_response(200, service.mark_departed(
                actor_id=actor_id, departure_id=departure_id,
                request_id=body.get("request_id", "")))
        if method == "GET" and segments[2] == "entitlements":
            return 200, {"items": service.list_entitlements(departure_id)}
        if method == "GET" and segments[2] == "runs":
            return 200, {"items": service.run_history(departure_id)}
        if method == "GET" and segments[2] == "fairness":
            selectors = []
            for token in query.get("policy", []):
                if ":" in token:
                    policy_id, version = token.split(":", 1)
                    selectors.append({"policy_id": policy_id, "policy_version": int(version)})
                else:
                    selectors.append({"policy_id": token})
            return 200, service.compare_fairness(departure_id, selectors or None)

    if len(segments) == 2 and segments[0] == "departures" and method == "GET":
        return 200, service.get_departure(segments[1])

    if len(segments) == 3 and segments[0] == "demands":
        demand_id = segments[1]
        if method == "GET" and segments[2] == "explain":
            return 200, service.explain_demand(demand_id)
        if method == "POST" and segments[2] == "confirm":
            return receipt_response(200, service.confirm_demand(
                actor_id=actor_id, demand_id=demand_id, **body))
        if method == "POST" and segments[2] == "ship":
            return receipt_response(200, service.ship_demand(
                actor_id=actor_id, demand_id=demand_id, **body))
        if method == "POST" and segments[2] == "release":
            return receipt_response(200, service.release_demand(
                actor_id=actor_id, demand_id=demand_id,
                departure_id=body.get("departure_id", ""), request_id=body.get("request_id", "")))

    if len(segments) == 3 and segments[0] == "exceptions" and segments[2] == "decision":
        receipt = service.decide_exception(
            actor_id=actor_id, exception_id=segments[1], **body)
        payload = {"receipt": receipt}
        if not receipt.replayed:
            payload["exception"] = service.get_exception(segments[1])
        return 200, payload

    if len(segments) == 2 and segments[0] == "exceptions" and method == "GET":
        return 200, service.get_exception(segments[1])

    if len(segments) == 2 and segments[0] == "runs" and method == "GET":
        return 200, service.get_run(segments[1])

    if len(segments) == 3 and segments[0] == "runs" and segments[2] == "replay" and method == "POST":
        return 200, service.replay_run(segments[1], **body)

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

    parser = argparse.ArgumentParser(description="启动保供运力分配服务")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = SupplyService(database)
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
