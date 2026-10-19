from __future__ import annotations

import asyncio
import base64
import hmac
import ipaddress
import json
import secrets
import socket
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver
from standardwebhooks.webhooks import Webhook

from dots_phone.service import EVENT_NAMES, TERMINAL, Refused, digest


def signing_key(secret):
    if not isinstance(secret, str) or not secret.startswith("whsec_"):
        raise Refused("invalid subscription signing key")
    try:
        value = base64.b64decode(secret[6:], validate=True)
    except (ValueError, TypeError) as exc:
        raise Refused("invalid subscription signing key") from exc
    if not 24 <= len(value) <= 64:
        raise Refused("invalid subscription signing key length")
    return value


class PublicResolver(AbstractResolver):
    async def resolve(self, host, port=443, family=socket.AF_UNSPEC):
        addresses = await asyncio.get_running_loop().getaddrinfo(
            host, port, type=socket.SOCK_STREAM
        )
        if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
            raise Refused("callback address must be public")
        return [
            {
                "hostname": host,
                "host": row[4][0],
                "port": port,
                "family": row[0],
                "proto": row[2],
                "flags": socket.AI_NUMERICHOST,
            }
            for row in addresses
        ]

    async def close(self):
        pass


class SafeWebhook:
    def __init__(self, hosts):
        self.hosts = hosts

    def validate_url(self, url):
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.fragment
            or parts.port not in {None, 443}
        ):
            raise Refused("callback requires HTTPS on port 443 without credentials")
        if parts.hostname not in self.hosts:
            raise Refused("callback host is not configured")
        try:
            address = ipaddress.ip_address(parts.hostname)
        except ValueError:
            pass
        else:
            if not address.is_global:
                raise Refused("callback address must be public")

    async def post(self, url, body, headers):
        self.validate_url(url)
        # New connector per send: DNS checked at each connection; aiohttp connects
        # to the resolver's validated numeric address with original TLS hostname.
        connector = aiohttp.TCPConnector(
            resolver=PublicResolver(), use_dns_cache=False, force_close=True
        )
        async with aiohttp.ClientSession(
            connector=connector, timeout=aiohttp.ClientTimeout(total=10), trust_env=False
        ) as client:
            async with client.post(
                url, data=body, headers=headers, allow_redirects=False
            ) as response:
                content = await response.content.read(4097)
                if len(content) > 4096:
                    raise Refused("callback response too large")
                return response.status, content


class Events:
    def __init__(self, service, sender=None):
        self.service = service
        self.store = service.store
        self.sender = sender or SafeWebhook(service.settings.callback_hosts)

    def catalog(self):
        filter_schema = {
            "type": "object",
            "properties": {"call_id": {"type": "string"}},
            "required": ["call_id"],
            "additionalProperties": False,
        }
        payload_schema = {
            "type": "object",
            "properties": {
                "call_id": {"type": "string"},
                "revision": {"type": "integer"},
                "state": {"type": "string"},
                "summary": {"type": "string"},
            },
            "required": ["call_id", "revision", "state", "summary"],
            "additionalProperties": False,
        }
        return {
            "events": [
                {
                    "name": name,
                    "description": (
                        "A terminal call outcome is available; retrieve authoritative confirmation, unresolved questions and usage using get_call_result."
                        if name in {"call.completed", "call.failed"}
                        else "Call-scoped decision status; retrieve authoritative state using get_call."
                    ),
                    "delivery": ["webhook"],
                    "inputSchema": filter_schema,
                    "payloadSchema": payload_schema,
                }
                for name in EVENT_NAMES
            ]
        }

    async def send(self, sub, event, id):
        body = json.dumps(event, separators=(",", ":")).encode()
        if len(body) > 256 * 1024:
            raise Refused("event payload too large")
        signed_at = datetime.now(UTC)
        signature = Webhook(sub["secret"]).sign(id, signed_at, body.decode("utf-8"))
        headers = {
            "Content-Type": "application/json",
            "webhook-id": id,
            "webhook-timestamp": str(int(signed_at.timestamp())),
            "webhook-signature": signature,
            "X-MCP-Subscription-Id": sub["id"],
        }
        return await self.sender.post(sub["url"], body, headers)

    async def subscribe(self, owner, client_id, params):
        delivery = params.get("delivery", {})
        self.store.put(
            "event_subscription_diagnostic",
            "latest",
            "system",
            {
                "at": time.time(),
                "phase": "validating",
                "parameter_fields": sorted(params),
                "delivery_fields": sorted(delivery) if isinstance(delivery, dict) else [],
                "callback_host": urlsplit(delivery.get("url", "")).hostname
                if isinstance(delivery, dict)
                else None,
            },
        )
        if (
            params.get("name") not in EVENT_NAMES
            or set(params.get("arguments", {})) != {"call_id"}
            or params.get("cursor") is not None
        ):
            raise Refused("invalid event or filter; replay cursor unsupported")
        call_id = params["arguments"]["call_id"]
        if self.service.call(owner, call_id)["state"] in TERMINAL:
            raise Refused("call monitoring is finished")
        delivery = params.get("delivery", {})
        if set(delivery) != {"mode", "url", "secret"} or delivery["mode"] != "webhook":
            raise Refused("webhook delivery required")
        signing_key(delivery["secret"])
        self.sender.validate_url(delivery["url"])
        id = "sub_" + digest(
            [owner, client_id, delivery["url"], params["name"], params["arguments"]]
        )
        sub = {
            "id": id,
            "owner": owner,
            "client_id": client_id,
            "name": params["name"],
            "call_id": call_id,
            "url": delivery["url"],
            "secret": delivery["secret"],
            "expires": time.time() + 86400,
            "active": True,
        }
        challenge = secrets.token_urlsafe(32)
        status, body = await self.send(
            sub,
            {"type": "verification", "challenge": challenge},
            "msg_verification_" + secrets.token_hex(16),
        )
        observation = self.store.get("event_subscription_diagnostic", "latest")
        observation.update(phase="callback_replied", callback_status=status)
        self.store.put("event_subscription_diagnostic", "latest", "system", observation)
        try:
            echoed = json.loads(body).get("challenge", "")
        except (ValueError, AttributeError):
            echoed = ""
        if (
            not 200 <= status < 300
            or not isinstance(echoed, str)
            or not hmac.compare_digest(echoed, challenge)
        ):
            raise Refused("challenge_failed")
        self.store.put("subscription", id, owner, sub)
        observation.update(phase="active")
        self.store.put("event_subscription_diagnostic", "latest", "system", observation)
        return {
            "id": id,
            "refreshBefore": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            "cursor": None,
            "truncated": False,
        }

    def unsubscribe(self, owner, client_id, identity):
        if isinstance(identity, dict):
            # The documented wire identity omits the signing secret; a client
            # cannot revoke another client's otherwise identical subscription.
            delivery = identity.get("delivery", {})
            if (
                identity.get("name") not in EVENT_NAMES
                or set(identity.get("arguments", {})) != {"call_id"}
                or set(delivery) != {"mode", "url"}
                or delivery.get("mode") != "webhook"
            ):
                raise Refused("invalid unsubscribe identity")
            self.sender.validate_url(delivery["url"])
            id = "sub_" + digest(
                [owner, client_id, delivery["url"], identity["name"], identity["arguments"]]
            )
        else:
            id = identity
        sub = self.store.get("subscription", id, owner)
        if sub and sub["client_id"] == client_id:
            sub["active"] = False
            sub["secret"] = ""
            self.store.put("subscription", id, owner, sub)
            self.store.db.execute(
                "UPDATE deliveries SET status='revoked' WHERE sub_id=? AND status='pending'", (id,)
            )
        return {}

    def revoke_client(self, client_id):
        for sub in self.store.all("subscription"):
            if sub["client_id"] == client_id:
                self.unsubscribe(sub["owner"], client_id, sub["id"])

    async def tick(self):
        rows = self.store.db.execute(
            "SELECT * FROM deliveries WHERE status='pending' AND due<=? ORDER BY due LIMIT 20",
            (time.time(),),
        ).fetchall()
        for row in rows:
            sub = self.store.get("subscription", row["sub_id"])
            if not sub or not sub["active"] or sub["expires"] <= time.time():
                state, status = "revoked", 0
            else:
                try:
                    status, _ = await self.send(
                        sub, self.store.decode(row["payload"]), row["event_id"]
                    )
                except Exception:
                    status = 0
                state = "delivered" if 200 <= status < 300 else "pending"
                if status in {401, 403, 404, 410}:
                    self.unsubscribe(sub["owner"], sub["client_id"], sub["id"])
                    state = "revoked"
                if row["attempts"] >= 11 and state == "pending":
                    state = "failed"
            self.store.db.execute(
                "UPDATE deliveries SET status=?,attempts=attempts+1,due=? WHERE event_id=? AND sub_id=?",
                (
                    state,
                    time.time() + min(3600, 2 ** (row["attempts"] + 1)),
                    row["event_id"],
                    row["sub_id"],
                ),
            )
        self.close_completed_monitoring()

    def close_completed_monitoring(self):
        groups = {}
        for sub in self.store.all("subscription"):
            if sub["active"]:
                groups.setdefault((sub["owner"], sub["client_id"], sub["call_id"]), []).append(sub)
        for (owner, client_id, call_id), subscriptions in groups.items():
            call = self.store.get("call", call_id, owner)
            if (
                not call
                or call["state"] not in TERMINAL
                or call["usage"].get("voice_closed_confirmed") is not True
            ):
                continue
            expected = "call.failed" if call["state"] == "failed" else "call.completed"
            terminal = [s for s in subscriptions if s["name"] == expected]
            if not terminal or not all(
                self.store.db.execute(
                    "SELECT 1 FROM (SELECT status FROM deliveries WHERE sub_id=? "
                    "ORDER BY rowid DESC LIMIT 1) WHERE status='delivered'",
                    (sub["id"],),
                ).fetchone()
                for sub in terminal
            ):
                continue
            with self.store.transaction():
                for sub in subscriptions:
                    self.unsubscribe(owner, client_id, sub["id"])

    async def run(self):
        while True:
            await self.tick()
            await asyncio.sleep(1)
