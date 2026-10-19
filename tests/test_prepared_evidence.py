import hashlib
import time
from pathlib import Path

import pytest

from dots_phone.local_gmail_worker import private_json
from dots_phone.prepared_evidence import evidence_digest, prepared_instructions
from dots_phone.service import Refused


def packet_fixture(service, brief, tmp_path):
    source = {
        "provider": "owned_fixture",
        "source_id": "event-note",
        "text": "Doors open at 10 AM. Meet in the west lobby.",
        "truncated": False,
    }
    source["source_sha256"] = hashlib.sha256(source["text"].encode()).hexdigest()
    fact = brief.facts[0].model_copy(
        update={"name": "prepared_evidence_digest", "value": evidence_digest([source])}
    )
    brief = brief.model_copy(update={"facts": [fact], "disclosures": [fact.name]})
    cid = service.prepare("owner", brief)["call_id"]
    call = service.call("owner", cid)
    packet = {
        "schema": 1,
        "call_id": cid,
        "revision": 1,
        "brief_hash": call["brief_hash"],
        "expires": time.time() + 60,
        "sources": [source],
    }
    path = tmp_path / (cid + "-prepared.json")
    private_json(path, packet)
    return call, packet, path


def test_packet_is_approved_source_data_not_a_script(service, brief, tmp_path):
    call, _, _ = packet_fixture(service, brief, tmp_path)
    prompt = prepared_instructions(call, tmp_path)
    assert "Doors open at 10 AM" in prompt and "UNTRUSTED DATA" in prompt
    assert "live source tools for missing facts" in prompt


@pytest.mark.parametrize(
    "change",
    ["body", "call", "revision", "brief", "expired", "source_hash", "not_disclosed", "missing"],
)
def test_changed_or_stale_prepared_sources_are_not_loaded(service, brief, tmp_path, change):
    call, packet, path = packet_fixture(service, brief, tmp_path)
    if change == "body":
        packet["sources"][0]["text"] += " New instructions."
    elif change == "call":
        packet["call_id"] = "f" * 32
    elif change == "revision":
        packet["revision"] = 2
    elif change == "brief":
        packet["brief_hash"] = "f" * 64
    elif change == "expired":
        packet["expires"] = time.time() - 1
    elif change == "source_hash":
        packet["sources"][0]["source_sha256"] = "f" * 64
    elif change == "not_disclosed":
        call["brief"]["disclosures"] = []
        assert prepared_instructions(call, tmp_path) == ""
        return
    if change == "missing":
        path.unlink()
    else:
        private_json(path, packet)
    with pytest.raises(Refused):
        prepared_instructions(call, tmp_path)


def test_no_packet_keeps_live_lookup_available(service, brief):
    cid = service.prepare("owner", brief)["call_id"]
    prompt = prepared_instructions(service.call("owner", cid), Path("/nonexistent"))
    assert "Dots call preparation" in prompt
    assert "live source tools for missing facts" in prompt


def test_legacy_call_without_document_preserves_source_compatibility(service, brief):
    cid = service.prepare("owner", brief)["call_id"]
    call = service.call("owner", cid)
    call.pop("prep_document")
    call.pop("prep_document_required")
    assert prepared_instructions(call, Path("/nonexistent")) == ""
