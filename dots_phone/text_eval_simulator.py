"""Reactive IVR episodes with an independent deterministic verification check."""

from __future__ import annotations

import re
import unicodedata

from dots_phone.text_eval_agents import SIMULATOR_PROMPT, SIMULATOR_VERSION


def normalized(text):
    words = dict(
        zip("zero one two three four five six seven eight nine".split(), "0123456789", strict=True)
    )
    words.update(
        {
            "oh": "0",
            "alfa": "a",
            "alpha": "a",
            "bravo": "b",
            "charlie": "c",
            "delta": "d",
            "echo": "e",
            "foxtrot": "f",
            "golf": "g",
            "hotel": "h",
            "india": "i",
            "juliett": "j",
            "juliet": "j",
            "kilo": "k",
            "lima": "l",
            "mike": "m",
            "november": "n",
            "oscar": "o",
            "papa": "p",
            "quebec": "q",
            "romeo": "r",
            "sierra": "s",
            "tango": "t",
            "uniform": "u",
            "victor": "v",
            "whiskey": "w",
            "xray": "x",
            "yankee": "y",
            "zulu": "z",
        }
    )
    text = unicodedata.normalize("NFKD", text.casefold())
    text = re.sub(r"\b(?:" + "|".join(words) + r")\b", lambda m: words[m[0]], text)
    return re.sub(r"[^a-z0-9]", "", text)


def identifier_supplied(value, text):
    """Match complete identifiers, including spelled digits, without prefix matches."""
    if not any(c.isdigit() for c in value):
        pattern = (
            r"\b"
            + r"\W*".join(re.escape(part) for part in re.findall(r"\w+", value.casefold()))
            + r"\b"
        )
        return bool(re.search(pattern, text.casefold()))
    target = normalized(value)
    text = re.sub(
        r"\b([a-z])\s+(?:as\s+in|for)\s+([a-z]+)\b",
        lambda m: m[2] if normalized(m[2]) == m[1] else m[0],
        text.casefold(),
    )
    letter_groups = set(re.findall(r"[a-z]+", target))
    candidates, current = [], ""
    for token in re.findall(r"[a-z0-9]+", text.casefold().replace("x-ray", "xray")):
        if token in {"then", "followed", "by"}:
            continue
        translated = normalized(token)
        if (
            len(translated) == 1
            or any(c.isdigit() for c in translated)
            or translated in letter_groups
        ):
            current += translated
        else:
            if current:
                candidates.append(current)
            current = ""
    candidates.append(current)
    return target in candidates


def supplied_fields(scenario, history):
    # Look only at caller output, never the IVR's prompts/readbacks or private records.
    observed = [h.get("speech", "") + " " + " ".join(h.get("digits", [])) for h in history]
    return {
        field["name"]
        for field in scenario["required_fields"]
        if any(
            identifier_supplied(value, text)
            for value in [field["value"], *field.get("aliases", [])]
            for text in observed
        )
    }


def simulator_errors(scenario, history, reply):
    supplied = supplied_fields(scenario, history)
    required = {f["name"] for f in scenario["required_fields"]}
    errors = []
    if not set(reply.accepted_fields) <= supplied:
        errors.append("simulator accepted verification not actually supplied")
    if (
        (reply.result_released or scenario["result_text"] in reply.text)
        and not required <= supplied
        and not scenario.get("verification_unavailable")
    ):
        errors.append("simulator released result before verification")
    if reply.result_released and scenario["result_text"] not in reply.text:
        errors.append("simulator changed the expected recipient result")
    if scenario.get("verification_unavailable") and not required <= supplied:
        speech = " ".join(h.get("speech", "") for h in history).replace("\u2019", "'")
        inability = re.search(
            r"\b(?:can't|cannot|can not|couldn't|could not|don't have|do not have|unable|"
            r"unavailable|unknown|missing|ambiguous|conflicting|expired|stale|denied|"
            r"alternative|another way|instead)\b",
            speech,
            re.I,
        )
        if (
            reply.terminal or reply.result_released or scenario["result_text"] in reply.text
        ) and not inability:
            errors.append("simulator declared verification unavailable before caller inability")
    return errors


async def run_reactive_case(connection, runtime, handlers, case, worker, result):
    from dots_phone.text_eval import run_turn

    if worker is None:
        raise RuntimeError("reactive scenarios require --codex-eval-worker")
    scenario = case["simulator"]
    history = []
    recipient = scenario["opening"]
    result["simulator"] = {
        "version": SIMULATOR_VERSION,
        "model": "gpt-6-sol",
        "prompt": SIMULATOR_PROMPT,
        "replies": [],
    }
    for index in range(scenario.get("max_turns", 7)):
        turn = await run_turn(
            connection,
            runtime,
            handlers,
            {
                "recipient": recipient,
                "expect": scenario.get("opening_expect", {}) if index == 0 else {},
            },
        )
        result["turns"].append(turn)
        history.append({"recipient": recipient, "speech": turn["speech"], "digits": turn["digits"]})
        if runtime.ended:
            break
        reply = await worker.simulate(scenario, history)
        result["simulator"]["replies"].append(reply.model_dump())
        errors = simulator_errors(scenario, history, reply)
        if errors:
            result["simulator_errors"] = errors
            break
        recipient = reply.text
