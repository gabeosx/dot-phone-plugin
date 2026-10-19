"""Shared phone phrase bank; automatic holding lines contain no lookup claims."""

import json
import secrets
from collections import deque
from functools import lru_cache
from pathlib import Path

_recent_holding = deque(maxlen=12)


def skill_reference(name):
    relative = Path("skills/dots-phone/references") / name
    packaged = Path(__file__).parent / "_plugin" / relative
    source = Path(__file__).resolve().parent.parent / "plugins/dots-phone" / relative
    return packaged if packaged.exists() else source


@lru_cache(maxsize=1)
def speech_library():
    library = json.loads(skill_reference("speech-library.json").read_text())
    if library.get("schema") != 1:
        raise ValueError("unsupported phone speech library")
    groups = [*library["holding"].values(), *library["conversation_examples"].values()]
    if any(
        not group or any(not isinstance(p, str) or not p.strip() or len(p) > 100 for p in group)
        for group in groups
    ):
        raise ValueError("invalid phone speech library")
    return library


def choose_holding_phrase(*, ivr_only=False):
    bank = speech_library()["holding"]
    pool = bank["short"] if ivr_only else [*bank["short"], *bank["conversational"]]
    candidates = [p for p in pool if p not in _recent_holding]
    if not candidates:
        candidates = [p for p in pool if not _recent_holding or p != _recent_holding[-1]] or pool
    phrase = secrets.choice(candidates)
    _recent_holding.append(phrase)
    return phrase


def speech_examples():
    library = speech_library()
    return (
        "\nPhone phrase library. These are options, not a sequence or mandatory filler. "
        "Use only wording appropriate to what was actually said. Known names and addresses "
        "need a direct answer, not a lookup story. The controller chooses neutral holding lines. "
        "Do not add extra acknowledgements to IVR topics, yes/no replies or exact identifiers.\n"
        + json.dumps(library, ensure_ascii=False)
    )
