"""Separate route-limited gateways; MCP and management never enter media ingress."""

from __future__ import annotations

import asyncio
import os
import re
from contextlib import asynccontextmanager

import aiohttp
import httpx
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import Response
from starlette.websockets import WebSocketDisconnect

from dots_phone.ingress_diagnostics import IngressDiagnostics

BACKEND = "http://phone-app:8000"
HOP_HEADERS = {"host", "content-length", "connection", "transfer-encoding", "keep-alive", "upgrade"}


def allowed(kind, method, path):
    if kind == "media":
        return method == "POST" and re.fullmatch(r"/twilio/status/[0-9a-f]{32}", path) is not None
    if kind != "owner":
        return False
    methods = {
        "/authorize": {"GET", "POST"},
        "/token": {"POST"},
        "/register": {"POST"},
        "/revoke": {"POST"},
        "/.well-known/oauth-authorization-server": {"GET"},
        "/.well-known/oauth-protected-resource": {"GET"},
        "/.well-known/oauth-protected-resource/mcp": {"GET"},
        "/owner/campaign/audio": {"GET", "POST"},
    }
    if path in methods:
        return method in methods[path]
    return method in {"GET", "POST"} and (
        re.fullmatch(r"/approve/[0-9a-f]{32}", path) is not None
        or re.fullmatch(r"/owner/campaign/phone/[0-9a-f]{32}(?:/revoke)?", path) is not None
        or re.fullmatch(r"/owner/consent/[A-Za-z0-9_-]{43}", path) is not None
    )


def create_gateway(kind=None, client=None):
    kind = kind or os.environ.get("DOTS_PHONE_INGRESS_KIND", "media")
    if kind not in {"media", "owner"}:
        raise ValueError("unknown ingress kind")

    @asynccontextmanager
    async def lifespan(app):
        app.state.backend = client or httpx.AsyncClient(
            timeout=20, follow_redirects=False, trust_env=False
        )
        try:
            yield
        finally:
            if client is None:
                await app.state.backend.aclose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    async def health():
        return {"status": "ok", "ingress": kind, "mcp_exposed": False}

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def forward(path, request: Request):
        if not allowed(kind, request.method, "/" + path):
            return Response(status_code=404)
        body = await request.body()
        if len(body) > 32768:
            return Response(status_code=413)
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_HEADERS}
        # A shared HTTP pool must never inject another browser's cookie jar.
        headers["cookie"] = request.headers.get("cookie", "")
        remote = await app.state.backend.request(
            request.method,
            BACKEND + "/" + path,
            params=request.query_params,
            content=body,
            headers=headers,
        )
        response = Response(content=remote.content, status_code=remote.status_code)
        response.raw_headers.extend(
            (k, v)
            for k, v in remote.headers.raw
            if k.decode().lower() not in HOP_HEADERS | {"content-encoding"}
        )
        return response

    @app.websocket("/voice")
    async def voice(websocket: WebSocket):
        if kind != "media" or websocket.url.query:
            await websocket.close(code=1008)
            return
        signature = websocket.headers.get("x-twilio-signature", "")
        if not signature:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        diagnostics = IngressDiagnostics()
        upstream_connected = False
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(
                    BACKEND.replace("http://", "ws://") + "/voice",
                    headers={"x-twilio-signature": signature},
                    max_msg_size=16384,
                ) as upstream:
                    upstream_connected = True

                    async def incoming():
                        try:
                            while True:
                                try:
                                    text = await websocket.receive_text()
                                except WebSocketDisconnect as error:
                                    diagnostics.end(
                                        "incoming", "carrier", "peer_disconnected", error.code
                                    )
                                    raise
                                except Exception:
                                    diagnostics.end("incoming", "carrier", "receive_failed")
                                    raise
                                size = len(text.encode())
                                if size > 16384:
                                    diagnostics.end(
                                        "incoming", "gateway", "message_size_limit", 1009
                                    )
                                    await websocket.close(code=1009)
                                    return
                                try:
                                    await upstream.send_str(text)
                                except Exception:
                                    diagnostics.end("incoming", "upstream", "send_failed")
                                    raise
                                diagnostics.forwarded_message("incoming", size)
                        finally:
                            diagnostics.end("incoming", "unknown", "cancelled")

                    async def outgoing():
                        try:
                            async for message in upstream:
                                if message.type == aiohttp.WSMsgType.TEXT:
                                    try:
                                        await websocket.send_text(message.data)
                                    except WebSocketDisconnect as error:
                                        diagnostics.end(
                                            "outgoing", "carrier", "send_failed", error.code
                                        )
                                        raise
                                    except Exception:
                                        diagnostics.end("outgoing", "carrier", "send_failed")
                                        raise
                                    diagnostics.forwarded_message(
                                        "outgoing", len(message.data.encode())
                                    )
                                else:
                                    outcome = (
                                        "peer_disconnected"
                                        if message.type
                                        in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED}
                                        else "receive_failed"
                                        if message.type == aiohttp.WSMsgType.ERROR
                                        else "non_text_message"
                                    )
                                    diagnostics.end(
                                        "outgoing", "upstream", outcome, upstream.close_code
                                    )
                                    return
                            diagnostics.end(
                                "outgoing", "upstream", "stream_ended", upstream.close_code
                            )
                        except Exception:
                            diagnostics.end(
                                "outgoing", "upstream", "receive_failed", upstream.close_code
                            )
                            raise
                        finally:
                            diagnostics.end("outgoing", "unknown", "cancelled")

                    tasks = [asyncio.create_task(incoming()), asyncio.create_task(outgoing())]
                    try:
                        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                        diagnostics.completed(
                            [
                                direction
                                for direction, task in zip(
                                    ("incoming", "outgoing"), tasks, strict=True
                                )
                                if task in done
                            ]
                        )
                    finally:
                        for task in tasks:
                            task.cancel()
                        # Drain both already-cancelled tasks before reporting.
                        # Preserve the original incoming/outgoing exception
                        # priority without leaving the peer unobserved.
                        results = await asyncio.gather(*tasks, return_exceptions=True)
                        for result in results:
                            if isinstance(result, BaseException) and not isinstance(
                                result, (asyncio.CancelledError, WebSocketDisconnect)
                            ):
                                raise result
        except (aiohttp.ClientError, WebSocketDisconnect):
            if not upstream_connected:
                diagnostics.setup_failed()
            pass
        finally:
            try:
                await websocket.close()
            except WebSocketDisconnect as error:
                # The peer may already be gone when bridge cleanup attempts a
                # close. Keep that fact separate from the first observed edge.
                diagnostics.cleanup("already_disconnected", error.code)
            except RuntimeError:
                diagnostics.cleanup("runtime_state_error")
            except BaseException:
                diagnostics.cleanup("failed")
                raise
            else:
                diagnostics.cleanup("completed")
            finally:
                diagnostics.log()

    return app
