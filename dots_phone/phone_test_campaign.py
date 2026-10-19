"""Standing owner consent for a bounded, exact synthetic phone-test brief."""

from __future__ import annotations

import re
import time
from decimal import Decimal

from dots_phone.service import Refused, digest


def brief_scope(brief):
    return {k: v for k, v in brief.items() if k not in {"deadline", "max_seconds", "budget_usd"}}


def provider_binding(settings):
    return digest(
        {"account": settings.twilio_account.get_secret_value(), "caller": settings.phone_number}
    )


def policy(service, owner, template_id):
    call = service.call(owner, template_id)
    brief = call["brief"]
    if (
        not brief["synthetic"]
        or brief["facts"]
        or brief["disclosures"]
        or brief["destination"] not in service.settings.allowed_numbers
        or not re.fullmatch(r"\+1[2-9][0-9]{9}", service.settings.phone_number)
        or not re.fullmatch(
            r"AC[0-9a-fA-F]{32}", service.settings.twilio_account.get_secret_value()
        )
        or brief["destination"] == service.settings.phone_number
    ):
        raise Refused(
            "standing approval is limited to distinct allowlisted synthetic test endpoints without personal facts"
        )
    seconds = min(90, brief["max_seconds"], service.settings.max_seconds)
    per_call_budget = min(Decimal("2"), Decimal(brief["budget_usd"]))
    return {
        "caller": service.settings.phone_number,
        "brief_scope": brief_scope(brief),
        "provider_binding": provider_binding(service.settings),
        "max_calls": 10,
        "per_call_max_seconds": seconds,
        "per_call_budget_usd": str(per_call_budget),
        "max_combined_seconds": 10 * 2 * (seconds + 30),
        "budget_usd": str(10 * per_call_budget),
        "valid_for_seconds": 86400,
    }


def validate(service, campaign, call):
    approved = campaign["test_policy"]
    brief = call["brief"]
    if (
        provider_binding(service.settings) != approved["provider_binding"]
        or brief_scope(brief) != approved["brief_scope"]
        or brief["max_seconds"] > approved["per_call_max_seconds"]
        or Decimal(brief["budget_usd"]) > Decimal(approved["per_call_budget_usd"])
        or not brief["synthetic"]
        or brief["facts"]
        or brief["disclosures"]
    ):
        raise Refused("call does not match the standing test approval")


def approve(service, owner, template_id):
    """Called only after the owner page authenticates its exact policy."""
    approved = policy(service, owner, template_id)
    with service.store.transaction():
        for old in service.store.all("campaign", owner):
            if old.get("test_policy") and not old["revoked"]:
                old["revoked"] = True
                service.store.put("campaign", old["id"], owner, old)
        campaign_id = service.approve_campaign(
            owner,
            {
                "destinations": [approved["brief_scope"]["destination"]],
                "expires": time.time() + approved["valid_for_seconds"],
                "max_calls": approved["max_calls"],
                "max_seconds": approved["max_combined_seconds"],
                "budget_usd": approved["budget_usd"],
                "scenarios": [digest(approved["brief_scope"])],
                "providers": ["openai", "twilio"],
            },
        )
        row = service.store.get("campaign", campaign_id, owner)
        row["test_policy"] = approved
        service.store.put("campaign", campaign_id, owner, row)
    return campaign_id


def authorize_prepared(service, owner, call):
    for campaign in sorted(
        service.store.all("campaign", owner), key=lambda c: c["expires"], reverse=True
    ):
        if (
            not campaign.get("test_policy")
            or campaign["revoked"]
            or campaign["expires"] <= time.time()
        ):
            continue
        try:
            validate(service, campaign, call)
            return service.campaign_authorization(
                owner, campaign["id"], call["call_id"], digest(brief_scope(call["brief"]))
            )
        except Refused:
            continue  # An exhausted/mismatched grant never authorizes this call.
    return None
