"""Authenticated private-controller proof with synthetic state and dialing disabled."""

import json
import os
from datetime import timedelta
from pathlib import Path

from dots_phone.bws import private_write
from dots_phone.config import Settings
from dots_phone.models import Brief, now
from dots_phone.server import create_app

settings = Settings.from_env()
if settings.profile != "evaluation" or not settings.dry_run:
    raise RuntimeError("controller fixture requires evaluation mode with dialing disabled")
app = create_app(settings)
# This is a test credential for a separate evaluation database, never production
# owner consent or permission to start a live call. The normal MCP verifier runs.
token_path = Path("/data/controller-proof-token")
if token_path.exists():
    token_path.unlink()
private_write(token_path, app.state.oauth.issue("tunnel-controller-fixture").access_token)
prepared = app.state.service.prepare(
    settings.owner,
    Brief(
        destination="+12125550100",
        purpose="Read-only private tunnel controller verification; fictional state only",
        deadline=now() + timedelta(minutes=15),
        max_seconds=30,
        budget_usd="1",
    ),
)
report = {
    "mode": "private_controller_evaluation",
    "dry_run": settings.dry_run,
    "providers_configured": False,
    "call_id": prepared["call_id"],
    "revision": prepared["revision"],
    "state": "prepared",
    "token_lifetime_seconds": 900,
    "human_approval_granted": False,
    "event_subscriptions_created": False,
    "call_started": False,
    "process_id": os.getpid(),
}
Path("/artifacts/controller-fixture.json").write_text(json.dumps(report, indent=2) + "\n")
