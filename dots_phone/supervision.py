"""Durable host-task references; native task state is attested by the host, not us."""

import time

from dots_phone.service import TERMINAL, Refused


def voice_closed(call):
    return call["usage"].get("voice_closed_confirmed") is True or (
        call["state"] in TERMINAL and not call.get("provider_sid") and not call.get("dial_started")
    )


def state(call):
    refs = call.get("host_supervision")
    closed = voice_closed(call)
    status = (
        "not_registered"
        if not refs
        else "reported_closed"
        if refs.get("cleanup_reported_at")
        else "cleanup_required"
        if call["state"] in TERMINAL and closed
        else "awaiting_call_closure"
        if call["state"] in TERMINAL
        else "registered"
    )
    return {
        "required": call.get("host_supervision_required", False),
        "state": status,
        "voice_closed": closed,
        "decision_task_id": refs["decision_task_id"] if refs else None,
        "terminal_task_id": refs["terminal_task_id"] if refs else None,
        "cleanup_basis": "host_reported_native_task_settings"
        if refs and refs.get("cleanup_reported_at")
        else None,
        "cleanup_policy": "automatically_pause_call_owned_handlers_after_voice_closure",
    }


def previous_terminal_calls(service, owner, current):
    """Give the host exact references for retry/task-capacity recovery, without source facts."""
    calls = sorted(service.store.all("call", owner), key=lambda c: c["created"], reverse=True)
    return [
        {"call_id": c["call_id"], "state": c["state"], "host_lifecycle": state(c)}
        for c in calls
        if c["call_id"] != current["call_id"]
        and c["brief"]["destination"] == current["brief"]["destination"]
        and c["state"] in TERMINAL
        and not c.get("host_supervision", {}).get("cleanup_reported_at")
    ][:3]


def register(service, owner, client_id, params):
    with service.store.transaction():
        call = service.call(owner, params.call_id)
        existing = call.get("host_supervision")
        refs = {
            "decision_task_id": params.decision_task_id,
            "terminal_task_id": params.terminal_task_id,
            "client_id": client_id,
        }
        if existing:
            if any(existing.get(k) != value for k, value in refs.items()):
                raise Refused("registered call handlers cannot be replaced")
        elif call["state"] != "prepared" and call["state"] not in TERMINAL:
            raise Refused("register call handlers before starting")
        else:
            selected = {params.decision_task_id, params.terminal_task_id}
            if any(
                other["call_id"] != call["call_id"]
                and selected
                & {
                    other.get("host_supervision", {}).get("decision_task_id"),
                    other.get("host_supervision", {}).get("terminal_task_id"),
                }
                for other in service.store.all("call", owner)
            ):
                raise Refused("native task is already bound to another call")
            call["host_supervision"] = {**refs, "registered_at": time.time()}
            service.save(call)
        return state(call)


def report_cleanup(service, owner, client_id, params):
    with service.store.transaction():
        call = service.call(owner, params.call_id)
        refs = call.get("host_supervision")
        if not refs or any(
            refs.get(k) != value
            for k, value in {
                "client_id": client_id,
                "decision_task_id": params.decision_task_id,
                "terminal_task_id": params.terminal_task_id,
            }.items()
        ):
            raise Refused("cleanup must match this connection's registered call handlers")
        if call["state"] not in TERMINAL or not voice_closed(call):
            raise Refused("wait for terminal call and confirmed voice closure before cleanup")
        refs.setdefault("cleanup_reported_at", time.time())
        refs["decision_task_enabled"] = False
        refs["terminal_task_enabled"] = False
        service.save(call)
        return state(call)
