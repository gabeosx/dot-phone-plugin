"""Private, read-only MCP evidence endpoint. No dialing or model-starting tool."""

import json
from contextlib import asynccontextmanager
from pathlib import Path

import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route


def status():
    path = Path("/artifacts/pipecat-offline.json")
    output = {"mode": "offline_protocol_fixture", "state": "not_run", "live_baseline": "unproven"}
    if path.exists():
        result = json.loads(path.read_text())
        output.update(passed=result["baseline_passed"], hard_assertions=result["hard_assertions"])
        output.pop("state")
    reports = sorted(
        Path("/artifacts").glob("pipecat-live-*/result.json"), key=lambda p: p.stat().st_mtime
    )
    if reports:
        live = json.loads(reports[-1].read_text())
        output["live_baseline"] = (
            "passed"
            if live["baseline_passed"]
            else "failed"
            if live["usage_confirmed"]
            else "unconfirmed"
        )
        output["live"] = {
            name: live.get(name)
            for name in (
                "state",
                "baseline_passed",
                "eval_passed",
                "usage_confirmed",
                "final_seconds",
                "hard_assertions",
            )
        }
    campaigns = sorted(
        Path("/artifacts").glob("repeatability-*/summary.json"), key=lambda p: p.stat().st_mtime
    )
    if campaigns:
        output["repeatability"] = json.loads(campaigns[-1].read_text())
        audit = campaigns[-1].with_name("audio-audit-summary.json")
        if audit.exists():
            output["audio_audit"] = json.loads(audit.read_text())
    inbound = Path("/artifacts/inbound-demo.json")
    if inbound.exists():
        output["inbound_test"] = json.loads(inbound.read_text())
    return output


async def list_tools(ctx, params):
    return types.ListToolsResult(
        tools=[
            types.Tool(
                name="get_baseline_status",
                description="Read the synthetic Pipecat experiment's status and evidence limits.",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                annotations=types.ToolAnnotations(
                    read_only_hint=True, destructive_hint=False, open_world_hint=False
                ),
            )
        ]
    )


async def call_tool(ctx, params):
    if params.name != "get_baseline_status" or params.arguments:
        return types.CallToolResult(
            content=[types.TextContent(type="text", text="Invalid tool or arguments")],
            is_error=True,
        )
    result = status()
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(result))], structured_content=result
    )


mcp = Server(
    "dots-pipecat-spike", version="0.1.0", on_list_tools=list_tools, on_call_tool=call_tool
)
mcp_app = mcp.streamable_http_app(
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["phone-app:8000", "127.0.0.1:*", "localhost:*", "testserver"],
    ),
)


@asynccontextmanager
async def lifespan(app):
    async with mcp.session_manager.run():
        yield


async def health(request):
    return JSONResponse({"service": "pipecat-spike", "paid_sessions_enabled": False})


app = Starlette(routes=[Route("/healthz", health), Mount("/", mcp_app)], lifespan=lifespan)
