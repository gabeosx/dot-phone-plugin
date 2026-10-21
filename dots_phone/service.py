from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
from decimal import Decimal
from uuid import uuid4

from dots_phone.call_budget import affordable_seconds, reservation_usd
from dots_phone.models import Brief, Decision, Start, Steer, now
from dots_phone.source_search import source_activity

EVENT_NAMES = ("call.decision_required", "call.completed", "call.failed")
TERMINAL = {"completed", "failed", "cancelled"}
ACTIVE = {"queued", "dialing", "active", "awaiting_decision", "stopping", "reconcile_required"}


class Refused(ValueError):
    pass


def handles_locally(worker, call):
    return bool(
        worker
        and worker.registered(call)
        and getattr(worker, "handles_pending", lambda _: True)(call)
    )


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def transcript_entry(entry, *, limit=None):
    text = entry.get("text", "")
    result = {"role": entry.get("role", ""), "text": text[:limit] if limit else text}
    if (
        result["role"] == "user"
        and type(entry.get("recipient_turn")) is int
        and entry["recipient_turn"] > 0
        and isinstance(entry.get("recipient_item_id"), str)
        and 0 < len(entry["recipient_item_id"]) <= 240
    ):
        result.update(
            recipient_turn=entry["recipient_turn"], recipient_item_id=entry["recipient_item_id"]
        )
    return result


def stop_state(reason):
    return {
        "owner_cancelled": "cancelled",
        "recipient_stop": "completed",
        "task_finished": "completed",
        "conversation_finished": "completed",
        "test_human_detected": "completed",
        "test_response_limit": "completed",
        "test_stop_condition": "completed",
    }.get(reason, "failed")


class PhoneService:
    def __init__(self, settings, store, provider=None):
        self.settings, self.store, self.provider = settings, store, provider
        self.wake = asyncio.Event()
        self.ready = False
        self.transcripts = {}
        self.maintenance_at = 0
        self.local_gmail_worker = None
        self.local_mail_worker = None
        self.local_context_worker = None
        from dots_phone.post_call_review import PostCallReviews

        self.post_call_reviews = PostCallReviews(self)

    def call(self, owner, call_id):
        result = self.store.get("call", call_id, owner)
        if result is None:
            raise Refused("call unavailable")
        return result

    def save(self, call):
        self.store.put("call", call["call_id"], call["owner"], call)

    def observe_transcript(self, call_id, entries, *, entry=None):
        # Preserve the active call's context even when transcript retention is
        # disabled. terminal() clears this transient history; archiving remains
        # governed by the call's capture policy.
        archive_entries = entries
        entries = [transcript_entry(e) for e in entries]
        self.transcripts[call_id] = entries
        call = self.store.get("call", call_id)
        if call and call.get("evidence_policy", {}).get(
            "transcript", self.settings.retain_transcript
        ):
            if entry is None:
                # Live's delta adapter supplies its complete growing dialog.
                call["transcript"] = [transcript_entry(e) for e in archive_entries]
            else:
                call["transcript"].append(
                    {**entry, "at_ms": round((time.time() - call["created"]) * 1000)}
                )
            self.save(call)

    def prepare(self, owner, brief: Brief, *, client_id=None):
        from dots_phone.prepared_evidence import create_prepared_document, normalize_preparation

        brief, evidence = normalize_preparation(brief)
        if brief.max_seconds is None:
            seconds = affordable_seconds(brief.budget_usd, self.settings.max_seconds)
            if seconds < 1:
                raise Refused("budget below conservative two-sided reservation")
            brief = brief.model_copy(update={"max_seconds": seconds})
        if self.settings.test_stop_on_human:
            response_limits = [
                limit
                for limit in (brief.test_response_limit, self.settings.test_response_limit)
                if limit
            ]
            brief = brief.model_copy(
                update={
                    "stop_on_human": True,
                    "test_response_limit": min(response_limits) if response_limits else 0,
                }
            )
        if brief.stop_on_human and (
            self.settings.voice_backend != "pipecat" or self.settings.pipecat_engine != "realtime"
        ):
            raise Refused("stop-on-human tests require Pipecat Realtime")
        if brief.test_response_limit and not brief.stop_on_human:
            raise Refused("test response limit requires stop-on-human test mode")
        connected_owner = (
            client_id is not None and self.settings.call_authorization_mode == "connected_owner"
        )
        if connected_owner:
            self.validate_connected_scope(owner, brief, client_id)
        current = now()
        if brief.deadline <= current:
            raise Refused("deadline expired")
        if brief.max_seconds > self.settings.max_seconds:
            raise Refused(
                f"duration exceeds configured bound ({self.settings.max_seconds} seconds)"
            )
        if not self.settings.destination_allowed(brief.destination):
            raise Refused("destination is outside configured calling policy")
        if any(
            f.expires_at <= current or f.verified_at > current or f.expires_at <= f.verified_at
            for f in brief.facts
        ):
            raise Refused("facts are stale or unverified")
        if len({f.name for f in brief.facts}) != len(brief.facts):
            raise Refused("duplicate fact names")
        if any(x not in {f.name for f in brief.facts} for x in brief.disclosures):
            raise Refused("disclosures must name supplied verified facts")
        payload = brief.model_dump(mode="json")
        if len(json.dumps(payload, ensure_ascii=False).encode()) > 4000:
            raise Refused("brief exceeds initial context byte budget")
        # Both voice streams/carrier legs + bounded evaluation/backend contingency.
        estimate = reservation_usd(brief.max_seconds)
        if brief.budget_usd < estimate:
            raise Refused("budget below conservative two-sided reservation")
        call = {
            "call_id": uuid4().hex,
            "owner": owner,
            "revision": 1,
            "brief": payload,
            "brief_hash": digest(payload),
            "state": "prepared",
            "created": time.time(),
            "estimate_usd": str(estimate),
            "reserved_usd": str(brief.budget_usd),
            "provider_sid": None,
            "pending": None,
            "result": None,
            "transcript": [],
            "evidence_policy": {
                "recording": self.settings.retain_recording,
                "transcript": self.settings.retain_transcript,
            },
            "progress": "Prepared; no call placed.",
            "usage": {"provider_seconds": 0, "actual_usd": None, "billing_confirmed": False},
        }
        if evidence:
            # The encrypted call record owns the packet. Preparation responses and
            # public approval pages carry its digest; authenticated get_call can
            # inspect the canonical document including these approved excerpts.
            call["prepared_evidence"] = {
                "schema": 1,
                "call_id": call["call_id"],
                "revision": call["revision"],
                "brief_hash": call["brief_hash"],
                **evidence,
            }
        # Legacy internal fixtures can bind an external packet after preparation.
        # MCP PrepareCall always supplies its sources here and cannot set this
        # reserved digest itself. Every new ordinary preparation has a document.
        if evidence or not any(f["name"] == "prepared_evidence_digest" for f in payload["facts"]):
            call["prep_document_required"] = True
            call["prep_document"] = create_prepared_document(call)
        from dots_phone.account_task import general_call_actions

        autonomous = self.settings.local_reasoner_enabled and (
            "read_account_information" in payload["actions"]
            or general_call_actions(payload["actions"])
        )
        if autonomous:
            call["runtime_supervision"] = "local_reasoner"
            call["reasoner_response_policy"] = self.settings.local_reasoner_response_policy
        self.save(call)
        from dots_phone.phone_test_campaign import authorize_prepared

        authorization = None
        if self.local_context_worker and hasattr(self.local_context_worker, "register_prepared"):
            authorization = self.local_context_worker.register_prepared(owner, call)
        if not authorization:
            authorization = authorize_prepared(self, owner, call)
        if not authorization and connected_owner:
            authorization = self.approve(owner, call["call_id"], client_id=client_id)
        current_call = self.call(owner, call["call_id"])
        account_task = (
            self.settings.account_tasks and "read_account_information" in payload["actions"]
        )
        shared_task = account_task or general_call_actions(payload["actions"])
        if (
            connected_owner
            and account_task
            and not autonomous
            and not current_call.get("campaign_id")
        ):
            current_call["host_supervision_required"] = True
            self.save(current_call)
        from dots_phone.supervision import previous_terminal_calls
        from dots_phone.supervision import state as supervision_state

        host_information = (
            account_task and not autonomous and "read_call_sources" in payload["actions"]
        )
        keypad = False
        if shared_task and hasattr(self.provider, "account_keypad_enabled"):
            try:
                keypad = self.provider.account_keypad_enabled(current_call)
            except Refused:
                pass
        warnings = []
        if account_task and not host_information and not autonomous:
            warnings.append(
                "This account call cannot request missing information during the conversation. "
                "Include read_call_sources before approval for an ordinary inquiry; omit it only "
                "when the user explicitly restricts the call to briefing facts and prepared evidence."
            )
        if (
            shared_task
            and not keypad
            and {"navigate_phone_menu", "verify_account_identity"} & set(payload["actions"])
        ):
            warnings.append(
                "Keypad navigation/verification is unavailable in this deployment. Enable scoped "
                "task keypad support before calling an IVR that requires digits; do not "
                "substitute spoken digits for key presses."
            )
        return {
            "call_id": call["call_id"],
            "revision": 1,
            "brief": payload,
            "brief_hash": call["brief_hash"],
            "estimate_usd": str(estimate),
            "evidence_policy": call["evidence_policy"],
            "information_capabilities": {
                "can_request_host_information": host_information,
                **(
                    {
                        "independent_reasoner": True,
                        "response_policy": call["reasoner_response_policy"],
                        "reasoning_model": self.settings.local_reasoner_model,
                        "runtime_sources": [
                            "prep_document",
                            "disclosed_briefing_facts",
                            "prepared_evidence",
                            *(
                                ["signed_in_codex_read_tools"]
                                if "read_call_sources" in payload["actions"]
                                else []
                            ),
                        ],
                        "can_ask_owner_during_call": False,
                    }
                    if autonomous
                    else {}
                ),
            },
            "interaction_capabilities": {
                "keypad_menu": keypad and "navigate_phone_menu" in payload["actions"],
                "keypad_verification": keypad and "verify_account_identity" in payload["actions"],
                "owner_voice_join": False,
            },
            "preparation_warnings": warnings,
            "host_lifecycle": supervision_state(current_call),
            "previous_terminal_calls": previous_terminal_calls(self, owner, current_call),
            "approval_requirements": [
                "authenticated_connected_owner"
                if connected_owner and not current_call.get("campaign_id")
                else "approved_test_campaign"
                if authorization
                else "authenticated_owner_approval",
                *([] if autonomous else ["three_active_call_event_subscriptions"]),
            ],
            "approval_url": None
            if authorization
            else self.settings.public_base_url + "/approve/" + call["call_id"],
            "authorization_id": authorization,
            "campaign_id": current_call.get("campaign_id"),
            "prepared_evidence": {
                "digest": next(
                    f["value"] for f in payload["facts"] if f["name"] == "prepared_evidence_digest"
                ),
                "source_count": len(evidence["sources"]),
                "expires": evidence["expires"],
            }
            if evidence
            else None,
            "prep_document": {
                "ready": True,
                "sha256": call["prep_document"]["sha256"],
                "expires": call["prep_document"]["expires"],
                "read_with": "get_call",
            }
            if call.get("prep_document")
            else None,
            "calling_worker_mode": "autonomous_local_reasoner"
            if autonomous
            else "automatic_owned"
            if self.local_context_worker
            and getattr(self.local_context_worker, "automatic_handoff", False)
            else "manual",
        }

    def validate_connected_scope(self, owner, brief, client_id):
        if (
            self.settings.call_authorization_mode != "connected_owner"
            or owner != self.settings.owner
            or not isinstance(client_id, str)
            or not client_id.strip()
        ):
            raise Refused("authenticated connected owner required")
        if brief.max_seconds > self.settings.max_seconds:
            raise Refused("duration exceeds configured bound")
        if brief.budget_usd > self.settings.connected_call_budget_usd:
            raise Refused("call budget exceeds connected-owner limit")
        if not self.settings.destination_allowed(brief.destination):
            raise Refused("destination is outside configured calling policy")

    def approve(self, owner, call_id, *, client_id=None):
        """Trusted owner-page or authenticated MCP operation, never a model approval flag."""
        call = self.call(owner, call_id)
        if call["state"] != "prepared":
            raise Refused("only prepared calls may be approved")
        if client_id is not None:
            self.validate_connected_scope(owner, Brief.model_validate(call["brief"]), client_id)
        id = secrets.token_urlsafe(24)
        auth = {
            "id": id,
            "owner": owner,
            "call_id": call_id,
            "revision": call["revision"],
            "hash": call["brief_hash"],
            "expires": min(
                time.time() + (3600 if client_id is not None else 300),
                Brief.model_validate(call["brief"]).deadline.timestamp(),
            ),
            "consumed": False,
            "authorization_source": "connected_owner" if client_id is not None else "owner_page",
        }
        if client_id is not None:
            auth["authorizing_client_id"] = client_id
        self.store.put("approval", id, owner, auth)
        call["authorization_id"] = id
        self.save(call)
        return id

    def approve_campaign(self, owner, campaign):
        if set(campaign) != {
            "destinations",
            "expires",
            "max_calls",
            "max_seconds",
            "budget_usd",
            "scenarios",
            "providers",
        }:
            raise Refused("campaign scope incomplete")
        if not campaign["destinations"] or any(
            n not in self.settings.allowed_numbers for n in campaign["destinations"]
        ):
            raise Refused("campaign destination unavailable")
        if not time.time() < campaign["expires"] <= time.time() + 86400:
            raise Refused("campaign expiry must be within 24 hours")
        if (
            not 0 < campaign["max_calls"] <= 24
            or not 0 < campaign["max_seconds"] <= 3600
            or not Decimal("0") < Decimal(str(campaign["budget_usd"])) <= Decimal("20")
        ):
            raise Refused("campaign limits exceed initial bounds")
        if campaign["providers"] != ["openai", "twilio"] or not campaign["scenarios"]:
            raise Refused("unsupported or empty campaign")
        campaign = {
            **campaign,
            "id": uuid4().hex,
            "owner": owner,
            "calls": 0,
            "seconds": 0,
            "reserved_usd": "0",
            "revoked": False,
        }
        self.store.put("campaign", campaign["id"], owner, campaign)
        return campaign["id"]

    def campaign_authorization(self, owner, campaign_id, call_id, scenario):
        call = self.call(owner, call_id)
        with self.store.transaction():
            scope = self.store.get("campaign", campaign_id, owner)
            if (
                not scope
                or scope["revoked"]
                or scope["expires"] < time.time()
                or not call["brief"]["synthetic"]
                or scenario not in scope["scenarios"]
                or call["brief"]["destination"] not in scope["destinations"]
            ):
                raise Refused("call outside approved campaign")
            if scope.get("test_policy"):
                from dots_phone.phone_test_campaign import validate

                validate(self, scope, call)
            existing = self.store.get("campaign_call", call_id, owner)
            if existing:
                auth = self.store.get("approval", existing["authorization_id"], owner)
                if (
                    not auth
                    or auth.get("campaign_id") != campaign_id
                    or auth["hash"] != call["brief_hash"]
                    or auth["revision"] != call["revision"]
                    or auth["expires"] <= time.time()
                ):
                    raise Refused("campaign authorization is stale")
                return auth["id"]
            seconds = 2 * (call["brief"]["max_seconds"] + 30)
            cost = Decimal(scope["reserved_usd"]) + Decimal(call["reserved_usd"])
            if (
                scope["calls"] + 1 > scope["max_calls"]
                or scope["seconds"] + seconds > scope["max_seconds"]
                or cost > Decimal(str(scope["budget_usd"]))
            ):
                raise Refused("campaign exhausted")
            scope.update(
                calls=scope["calls"] + 1, seconds=scope["seconds"] + seconds, reserved_usd=str(cost)
            )
            auth_id = self.approve(owner, call_id)
            auth = self.store.get("approval", auth_id, owner)
            auth.update(
                campaign_id=campaign_id,
                expires=min(
                    scope["expires"], Brief.model_validate(call["brief"]).deadline.timestamp()
                ),
            )
            self.store.put("approval", auth_id, owner, auth)
            current = self.call(owner, call_id)
            current["campaign_id"] = campaign_id
            self.save(current)
            self.store.put("campaign_call", call_id, owner, {"authorization_id": auth_id})
            self.store.put("campaign", campaign_id, owner, scope)
            return auth_id

    def get(self, owner, call_id):
        from dots_phone.supervision import state as supervision_state

        call = self.call(owner, call_id)
        return {
            "host_lifecycle": supervision_state(call),
            **{
                k: call.get(k)
                for k in (
                    "call_id",
                    "revision",
                    "brief_hash",
                    "state",
                    "progress",
                    "pending",
                    "authorization_id",
                    "campaign_id",
                    "usage",
                    "estimate_usd",
                    "reserved_usd",
                    "prep_document",
                    "reasoner_response_policy",
                )
            },
        }

    async def watch(self, owner, call_id):
        """Bounded long poll for live supervision; approval/terminal reads stay immediate."""
        current = self.get(owner, call_id)
        local_lookup = any(
            handles_locally(worker, self.call(owner, call_id))
            for worker in (
                self.local_gmail_worker,
                self.local_mail_worker,
                self.local_context_worker,
            )
        )
        if not self.settings.call_state_wait_seconds or current["state"] not in {
            "queued",
            "dialing",
            "active",
            "stopping",
        } | ({"awaiting_decision"} if local_lookup else set()):
            return current
        state = (
            current["state"],
            current["revision"],
            (current["pending"] or {}).get("request_id"),
        )
        deadline = time.monotonic() + self.settings.call_state_wait_seconds
        while time.monotonic() < deadline:
            await asyncio.sleep(min(0.1, max(0, deadline - time.monotonic())))
            current = self.get(owner, call_id)
            identity = (
                current["state"],
                current["revision"],
                (current["pending"] or {}).get("request_id"),
            )
            if identity != state or (current["pending"] and not local_lookup):
                return current
        return current

    @staticmethod
    def reported_result(call):
        result = call["result"]
        if (
            call["brief"].get("completion_criteria")
            and "record_call_result" in call["brief"]["actions"]
        ):
            from dots_phone.call_task import quoted_task_result

            result = quoted_task_result(result)
        return result

    def result(self, owner, call_id):
        from dots_phone.supervision import state as supervision_state

        call = self.call(owner, call_id)
        if call["state"] not in TERMINAL:
            raise Refused("terminal result not available")
        return {
            "call_id": call_id,
            **self.reported_result(call),
            "revision": call["revision"],
            "brief_hash": call["brief_hash"],
            "state": call["state"],
            "usage": call["usage"],
            "source_activity": source_activity(call),
            "transcript": call["transcript"]
            if call.get("evidence_policy", {}).get("transcript", self.settings.retain_transcript)
            else None,
            "recording": call.get("recording"),
            **(
                {"report_review_status": call["final_report_review"]["state"]}
                if call.get("final_report_review")
                else {}
            ),
            "host_lifecycle": supervision_state(call),
        }

    def subscriptions_ready(self, owner, call_id):
        call = self.call(owner, call_id)
        if call.get("runtime_supervision") == "local_reasoner":
            worker = self.local_context_worker
            # A persisted mode flag is not supervision. A registered autonomous
            # worker must own decisions; validate_start also checks its heartbeat.
            return bool(worker and getattr(worker, "autonomous", False) and worker.registered(call))
        active = {
            s["name"]
            for s in self.store.all("subscription", owner)
            if s["call_id"] == call_id and s["active"] and s["expires"] > time.time()
        }
        return set(EVENT_NAMES) <= active

    def connected_reserved_budget(self, owner):
        reserved = Decimal("0")
        for row in self.store.all("connected_call_reservation", owner):
            if row["expires"] <= time.time():
                continue
            call = self.store.get("call", row["call_id"], owner)
            # Prewarming precedes persisted dial intent and any carrier/voice
            # request. Keep the reservation record for audit, but do not charge
            # an attempt that definitively ended at that boundary. Missing or
            # ambiguous call records still consume their full reservation.
            if (
                call
                and call["state"] == "failed"
                and (call.get("result") or {}).get("termination_reason")
                == "reasoner_preparation_failed"
                and not call.get("provider_sid")
                and "dial_started" not in call
                and call["usage"].get("provider_seconds") == 0
                and not call["usage"].get("realtime_session_count")
                and not call["usage"].get("realtime_response_usage")
            ):
                continue
            reserved += Decimal(row["budget_usd"])
        return reserved

    def validate_start(self, owner, params: Start, *, require_workers=True, client_id=None):
        if not self.ready:
            raise Refused("startup reconciliation incomplete")
        if self.settings.dry_run:
            raise Refused("dry_run: dialing disabled")
        call = self.call(owner, params.call_id)
        approval = self.store.get("approval", params.authorization_id, owner)
        if approval and approval.get("authorizing_client_id") is not None:
            if client_id != approval["authorizing_client_id"]:
                raise Refused("call authorization belongs to another connected client")
        if call.get("started_authorization") == params.authorization_id:
            return call, approval, True
        if (
            call["state"] != "prepared"
            or call["revision"] != params.revision
            or not approval
            or approval["consumed"]
            or approval["expires"] <= time.time()
            or approval["call_id"] != params.call_id
            or approval["hash"] != call["brief_hash"]
            or approval["revision"] != params.revision
        ):
            raise Refused("valid bound owner authorization required")
        brief = Brief.model_validate(call["brief"])
        if brief.stop_on_human and (
            self.settings.voice_backend != "pipecat" or self.settings.pipecat_engine != "realtime"
        ):
            raise Refused("stop-on-human tests require Pipecat Realtime")
        if call.get("host_supervision_required"):
            refs = call.get("host_supervision")
            if not refs or refs.get("client_id") != client_id:
                raise Refused(
                    "register_call_handlers with the native decision and terminal task IDs "
                    "through this connection before starting"
                )
        if approval.get("authorization_source") == "connected_owner":
            self.validate_connected_scope(owner, brief, client_id)
            reserved = self.connected_reserved_budget(owner)
            if reserved + brief.budget_usd > self.settings.connected_daily_budget_usd:
                raise Refused("connected-owner rolling daily call budget exhausted")
        if (
            brief.deadline <= now()
            or any(f.expires_at <= now() for f in brief.facts)
            or not self.settings.destination_allowed(brief.destination)
        ):
            raise Refused("approved context has expired or destination revoked")
        if (
            call.get("prepared_evidence")
            or call.get("prep_document_required")
            or call.get("prep_document")
        ):
            from dots_phone.prepared_evidence import prepared_instructions

            prepared_instructions(call)
        if approval.get("campaign_id"):
            campaign = self.store.get("campaign", approval["campaign_id"], owner)
            if not campaign or campaign["revoked"] or campaign["expires"] <= time.time():
                raise Refused("campaign revoked or expired")
            if campaign.get("test_policy"):
                from dots_phone.phone_test_campaign import validate

                validate(self, campaign, call)
        if not self.subscriptions_ready(owner, params.call_id):
            raise Refused("subscribe before dialing")
        if require_workers:
            for worker in (
                self.local_gmail_worker,
                self.local_mail_worker,
                self.local_context_worker,
            ):
                if worker and worker.registered(call):
                    worker.require_ready(call)
        if (
            sum(c["state"] in ACTIVE for c in self.store.all("call"))
            >= self.settings.max_concurrent
        ):
            raise Refused("concurrency exhausted")
        return call, approval, False

    def start(self, owner, params: Start, *, client_id=None):
        with self.store.transaction():
            call, approval, already_started = self.validate_start(
                owner, params, client_id=client_id
            )
            if already_started:
                return self.get(owner, params.call_id)
            approval["consumed"] = True
            self.store.put("approval", approval["id"], owner, approval)
            if approval.get("authorization_source") == "connected_owner":
                self.store.put(
                    "connected_call_reservation",
                    call["call_id"],
                    owner,
                    {
                        "call_id": call["call_id"],
                        "budget_usd": call["brief"]["budget_usd"],
                        "expires": time.time() + 86400,
                    },
                )
            call.update(
                state="queued",
                started_authorization=params.authorization_id,
                started_revision=call["revision"],
                control_revision=call["revision"],
                progress="Queued.",
            )
            if self.settings.trace_realtime_stages:
                call["usage"].setdefault("startup_timings", {}).update(
                    queued_epoch_ms=time.time() * 1000
                )
            self.save(call)
        self.wake.set()
        return self.get(owner, params.call_id)

    def emit(self, call, name, summary):
        event = {
            "eventId": uuid4().hex,
            "name": name,
            "timestamp": now().isoformat(),
            "data": {
                "call_id": call["call_id"],
                "revision": call["revision"],
                "state": call["state"],
                "summary": summary,
            },
            "cursor": None,
        }
        for sub in self.store.all("subscription", call["owner"]):
            if (
                sub["call_id"] == call["call_id"]
                and sub["name"] == name
                and sub["active"]
                and sub["expires"] > time.time()
            ):
                self.store.db.execute(
                    "INSERT OR IGNORE INTO deliveries(event_id,sub_id,payload,due) VALUES(?,?,?,?)",
                    (event["eventId"], sub["id"], self.store.encode(event), time.time()),
                )

    def terminal(self, call, state, reason):
        if call["state"] in TERMINAL:
            return
        from dots_phone.report_review import terminal_review_uncertainty

        uncertainty = terminal_review_uncertainty(call) if not call.get("conclusion") else []
        call.update(
            state=state,
            pending=None,
            progress="Call ended: " + reason,
            terminal_at=time.time(),
            result=call.get("conclusion")
            or {
                "outcome": "unresolved",
                "confirmed_facts": [],
                "commitments": [],
                "unresolved_questions": [
                    "The conversation is closed; final report review is pending."
                    if call.get("final_report_review", {}).get("state") == "pending"
                    else "The call ended before a verified final task report was recorded."
                    if reason == "completed"
                    else reason,
                    *uncertainty,
                ],
                "termination_reason": reason,
            },
        )
        call["result"]["termination_reason"] = reason
        if (
            call.get("runtime_supervision") == "local_reasoner"
            and self.settings.local_reasoner_directory
        ):
            from dots_phone.local_gmail_worker import private_json

            try:
                private_json(
                    self.settings.local_reasoner_directory
                    / (call["call_id"] + "-reason-closed.json"),
                    {
                        "call_id": call["call_id"],
                        "brief_hash": call["brief_hash"],
                    },
                )
            except OSError:
                call["usage"]["reasoner_cleanup_signal_failed"] = True
        self.transcripts.pop(call["call_id"], None)
        with self.store.transaction():
            self.save(call)
            self.emit(
                call,
                "call.completed" if state in {"completed", "cancelled"} else "call.failed",
                self.terminal_summary(call),
            )

    @staticmethod
    def terminal_summary(call):
        """Useful notification data in the existing event shape, never instructions."""
        result, usage = PhoneService.reported_result(call), call["usage"]
        confirmed = "; ".join(f["text"] for f in result.get("confirmed_facts", []))[:500]
        return json.dumps(
            {
                "outcome": result["outcome"],
                "termination_reason": result["termination_reason"],
                "confirmed": confirmed,
                "unresolved_count": len(result.get("unresolved_questions", [])),
                "voice_closed": usage.get("voice_closed_confirmed"),
                "usage_confirmed": usage.get("realtime_usage_confirmed"),
                "billing_confirmed": usage.get("billing_confirmed", False),
            },
            ensure_ascii=False,
        )

    async def end(self, owner, call_id, reason="owner_cancelled"):
        call = self.call(owner, call_id)
        if call["state"] in TERMINAL:
            return self.get(owner, call_id)
        if call["state"] in {"prepared", "queued"}:
            self.terminal(call, "cancelled", reason)
        else:
            if call["state"] == "stopping" and call.get("stop_retry_after", 0) > time.time():
                return self.get(owner, call_id)
            call.update(state="stopping", pending=None, stop_reason=reason)
            self.save(call)
            if call["provider_sid"]:
                try:
                    await self.provider.stop(call)
                except Exception:
                    call = self.call(owner, call_id)
                    attempts = call.get("stop_attempts", 0) + 1
                    call.update(
                        stop_attempts=attempts,
                        stop_retry_after=time.time() + min(60, 2 ** min(attempts, 6)),
                        progress="Carrier teardown unconfirmed; reserved and retrying with backoff.",
                    )
                    self.save(call)
                    self.wake.set()
                else:
                    self.terminal(
                        self.call(owner, call_id),
                        stop_state(reason),
                        reason,
                    )
        return self.get(owner, call_id)

    def pending_decision(self, call_id, delegation_id, question, *, context=None):
        call = self.store.get("call", call_id)
        if not call or call["state"] not in {"active", "awaiting_decision"}:
            return None
        if call["pending"] and call["pending"]["delegation_id"] == delegation_id:
            return call["pending"]
        pending = {
            "request_id": uuid4().hex,
            "revision": call["revision"],
            "delegation_id": delegation_id,
            "question": question,
            "expires": time.time() + self.settings.decision_seconds,
        }
        if context is not None:
            pending["context"] = context
        call.update(
            state="awaiting_decision",
            pending=pending,
            progress="Independent call reasoning in progress."
            if call.get("runtime_supervision") == "local_reasoner"
            else "Waiting for a scoped Dots decision.",
        )
        with self.store.transaction():
            self.save(call)
            if call.get("runtime_supervision") != "local_reasoner":
                self.emit(
                    call,
                    "call.decision_required",
                    "A bounded decision is required; retrieve get_call.",
                )
        return pending

    async def decision(self, owner, params: Decision):
        call = self.call(owner, params.call_id)
        pending = call["pending"]
        if (
            call["state"] != "awaiting_decision"
            or not pending
            or pending["request_id"] != params.request_id
            or pending["revision"] != params.revision
            or call["revision"] != params.revision
            or pending["expires"] <= time.time()
        ):
            raise Refused("decision is stale or unavailable")
        if not set(params.fact_names) <= set(call["brief"]["disclosures"]) or not set(
            params.action_names
        ) <= set(call["brief"]["actions"]):
            raise Refused("decision expands approved scope")
        worker = self.local_context_worker
        if worker and worker.registered(call) and hasattr(worker, "accept_source_decision"):
            worker.accept_source_decision(call, params)
        elif params.source is not None:
            raise Refused("this call has no live source capability")
        # The answer supplies data, never a new grant; voice prompt treats it as such.
        if params.conclusion:
            transcript = self.transcripts.get(call["call_id"], call.get("transcript", []))
            self.check_report_delivery(call)
            for evidence, role in [(e, "user") for e in params.conclusion.confirmed_facts] + [
                (e, "assistant") for e in params.conclusion.commitments
            ]:
                if not any(evidence.quote in e["text"] and e["role"] == role for e in transcript):
                    raise Refused("conclusion lacks observed transcript evidence")
                if role == "assistant" and evidence.action_name not in call["brief"]["actions"]:
                    raise Refused("commitment outside approved actions")
        self.save(call)
        await self.provider.update(call, pending["delegation_id"], params.answer)
        call = self.call(owner, params.call_id)
        if (
            call["state"] != "awaiting_decision"
            or not call["pending"]
            or call["pending"]["request_id"] != params.request_id
        ):
            raise Refused("call changed while submitting decision")
        if params.conclusion:
            self.check_report_delivery(call)
        call.update(
            state="active", pending=None, progress="Scoped answer forwarded; playback unconfirmed."
        )
        if params.conclusion:
            call["conclusion"] = params.conclusion.model_dump(mode="json")
        self.save(call)
        return self.get(owner, params.call_id)

    def check_report_delivery(self, call):
        expected = (call.get("pending") or {}).get("report_review_snapshot_sha256")
        if expected is not None:
            from dots_phone.report_review import recipient_review_snapshot

            entries = self.transcripts.get(call["call_id"], call.get("transcript", []))
            if digest(recipient_review_snapshot(call, entries)) != expected:
                raise Refused("recipient evidence changed during report delivery")

    async def steer(self, owner, params: Steer):
        call = self.call(owner, params.call_id)
        if (
            call["state"] not in {"active", "awaiting_decision"}
            or call["revision"] != params.revision
            or call.get("control_revision", call["revision"]) != params.revision
        ):
            raise Refused("stale steering request")
        await self.provider.steer(call, params.instruction)
        call = self.call(owner, params.call_id)
        if (
            call["state"] not in {"active", "awaiting_decision"}
            or call["revision"] != params.revision
            or call.get("control_revision", call["revision"]) != params.revision
        ):
            raise Refused("call changed while steering")
        # Steering invalidates old decisions, while the original start grant
        # continues to bind the unchanged brief. Keep those revisions separate.
        call.setdefault("started_revision", call["revision"])
        call.update(
            revision=call["revision"] + 1,
            control_revision=call["revision"] + 1,
            pending=None,
            state="active",
        )
        # A report repair is bound to its original scope. Revised calls must
        # obtain a new semantic review under the newly steered conversation.
        call.pop("report_review_state", None)
        self.save(call)
        return self.get(owner, params.call_id)

    async def callback(self, call_id, sid, status, duration=None):
        call = self.store.get("call", call_id)
        if not call:
            return
        if call["state"] in TERMINAL:
            # Media teardown can finish before the authenticated carrier callback.
            # Reconcile measured duration without reopening the task or emitting
            # another terminal event. Duration is usage, not confirmed billing.
            if not call["provider_sid"] or call["provider_sid"] != sid:
                raise Refused("provider SID mismatch")
            if duration is not None:
                call["usage"]["provider_seconds"] = max(
                    call["usage"]["provider_seconds"], int(duration)
                )
                self.save(call)
            return
        if call["provider_sid"] not in {None, sid}:
            raise Refused("provider SID mismatch")
        if call["state"] not in ACTIVE:
            raise Refused("unexpected provider callback")
        call["provider_sid"] = sid
        if duration is not None:
            call["usage"]["provider_seconds"] = max(
                call["usage"]["provider_seconds"], int(duration)
            )
        if status in {"completed", "busy", "failed", "no-answer", "canceled"}:
            self.save(call)
            await self.provider.close_voice(call)
            # Voice finalization can persist final usage while we await it.
            # Reload before terminal() so an older snapshot cannot erase it.
            current = self.store.get("call", call_id)
            if current["state"] in TERMINAL:
                return
            stop_reason = current.get("stop_reason") if current["state"] == "stopping" else None
            self.terminal(
                current,
                stop_state(stop_reason)
                if stop_reason
                else ("completed" if status == "completed" else "failed"),
                stop_reason or status,
            )
        elif call["state"] in {"stopping", "reconcile_required"}:
            self.save(call)
            await self.end(call["owner"], call_id, call.get("stop_reason", "ambiguous_dial"))
        else:
            # Preserve decision state; late ringing cannot regress an active call.
            if status == "in-progress" and call["state"] in {"queued", "dialing"}:
                call.update(state="active", active_at=time.time())
            self.save(call)

    def confirm_voice_closed(self, call_id, close_code):
        """Socket closure is independent of graceful shutdown or complete usage."""
        if type(close_code) is not int or not 1000 <= close_code <= 4999:
            return
        call = self.store.get("call", call_id)
        if not call:
            return
        already_closed = call["usage"].get("voice_closed_confirmed") is True
        call["usage"].update(voice_closed_confirmed=True, realtime_close_code=close_code)
        self.save(call)
        if not already_closed and call["state"] in TERMINAL:
            # A failure result can precede socket finalization. Wake the same
            # terminal handler again so cleanup does not depend on polling.
            self.emit(
                call,
                "call.failed" if call["state"] == "failed" else "call.completed",
                self.terminal_summary(call),
            )

    async def recover(self):
        for call in self.store.all("call"):
            if (
                call["state"] in TERMINAL
                and call["usage"].get("voice_closed_confirmed") is not True
            ):
                # Recover persisted disconnection evidence from older runtimes;
                # do not infer final usage or successful inquiry from closure.
                self.confirm_voice_closed(call["call_id"], call["usage"].get("realtime_close_code"))
                call = self.call(call["owner"], call["call_id"])
                if call["usage"].get("voice_closed_confirmed") is not True and call.get(
                    "final_report_review", {}
                ).get("state") in {"pending", "running"}:
                    # The dead process can no longer deliver its socket-close
                    # callback. Do not leave the shared worker queue blocked or
                    # replay a review whose voice evidence is incomplete.
                    self.post_call_reviews.fail(
                        call,
                        "Service restarted before voice closure was confirmed; "
                        "proposed claims remain unverified.",
                    )
            if call["state"] in ACTIVE - {"queued"}:
                if call["provider_sid"] and self.provider:
                    await self.end(call["owner"], call["call_id"], "server_restart")
                else:
                    call.update(
                        state="reconcile_required",
                        pending=None,
                        stop_reason="ambiguous_dial_after_restart",
                    )
                    self.save(call)
        self.ready = True

    async def tick(self):
        self.post_call_reviews.dispatch()
        local = self.local_context_worker
        if local and hasattr(local, "reconcile_handoffs"):
            local.reconcile_handoffs()
        if local and hasattr(local, "dispatch_start_requests"):
            await local.dispatch_start_requests()
        for call in self.store.all("call"):
            state = call["state"]
            if state == "queued":
                if self.post_call_reviews.outstanding():
                    call["progress"] = (
                        "Waiting for the previous call to close and finish result review."
                    )
                    self.save(call)
                    continue
                if local and local.registered(call) and hasattr(local, "before_dial"):
                    call["progress"] = "Preparing independent reasoning before dialing."
                    self.save(call)
                    try:
                        await local.before_dial(call)
                    except Exception:
                        current = self.call(call["owner"], call["call_id"])
                        if current["state"] == "queued":
                            self.terminal(current, "failed", "reasoner_preparation_failed")
                        continue
                    call = self.call(call["owner"], call["call_id"])
                    if call["state"] != "queued":
                        continue
                call.update(state="dialing", dial_started=time.time())
                if self.settings.trace_realtime_stages:
                    call["usage"].setdefault("startup_timings", {}).update(
                        carrier_dial_started_epoch_ms=time.time() * 1000
                    )
                self.save(call)  # Persist intent before any network operation.
                try:
                    sid = await self.provider.dial(call)
                except Exception:
                    call = self.call(call["owner"], call["call_id"])
                    if call["state"] not in TERMINAL:
                        call.update(
                            state="reconcile_required", stop_reason="ambiguous_dial", pending=None
                        )
                        self.save(call)  # No redial after ambiguous errors.
                    continue
                current = self.call(call["owner"], call["call_id"])
                if self.settings.trace_realtime_stages:
                    current["usage"].setdefault("startup_timings", {}).update(
                        carrier_dial_finished_epoch_ms=time.time() * 1000
                    )
                current["provider_sid"] = sid
                if current["state"] in TERMINAL:
                    try:
                        await self.provider.stop(current)
                    except Exception:
                        current.update(state="stopping", stop_reason="late_dial_cleanup")
                        self.save(current)
                elif current["state"] == "stopping":
                    self.save(current)
                    await self.end(
                        current["owner"],
                        current["call_id"],
                        current.get("stop_reason", "owner_cancelled"),
                    )
                else:
                    current.update(state="active", active_at=time.time())
                    self.save(current)
            elif state == "stopping" and call["provider_sid"]:
                await self.end(
                    call["owner"], call["call_id"], call.get("stop_reason", "owner_cancelled")
                )
            elif state in {"active", "awaiting_decision"}:
                if time.time() >= call["dial_started"] + call["brief"]["max_seconds"] + 30:
                    await self.end(call["owner"], call["call_id"], "duration_exhausted")
                elif call["pending"] and time.time() >= call["pending"]["expires"]:
                    await self.end(call["owner"], call["call_id"], "decision_timeout")
        if time.time() > self.maintenance_at + 60:
            self.maintenance_at = time.time()
            self.expire_records()

    def expire_records(self):
        cutoff = time.time() - self.settings.retention_seconds
        for call in self.store.all("call"):
            if call["state"] in TERMINAL and call.get("terminal_at", time.time()) < cutoff:
                for sub in self.store.all("subscription", call["owner"]):
                    if sub["call_id"] == call["call_id"]:
                        self.store.db.execute("DELETE FROM deliveries WHERE sub_id=?", (sub["id"],))
                        self.store.delete("subscription", sub["id"])
                self.store.delete("call", call["call_id"])
                self.store.delete("completion_diagnostic", call["call_id"])
                self.store.db.execute(
                    "DELETE FROM objects WHERE kind='call_audio' AND id LIKE ?",
                    (call["call_id"] + ":%",),
                )
        for kind in (
            "csrf",
            "approval",
            "oauth_transaction",
            "oauth_code",
            "oauth_access",
            "oauth_refresh",
            "campaign",
            "connected_call_reservation",
        ):
            for row in self.store.all(kind):
                expiry = row.get("expires", row.get("expires_at", time.time()))
                if expiry < cutoff:
                    # UUID/opaque IDs vary by record type; expired lookup uses encrypted table IDs.
                    for record in self.store.db.execute(
                        "SELECT id,payload FROM objects WHERE kind=?", (kind,)
                    ).fetchall():
                        if self.store.decode(record["payload"]) == row:
                            self.store.delete(kind, record["id"])

    async def run(self):
        while True:
            await self.tick()
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), 0.5)
            except TimeoutError:
                pass
