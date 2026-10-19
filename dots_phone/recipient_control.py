"""Recognize direct call-stop requests without executing quoted/menu instructions."""

import re

_STOP_PHRASE = re.compile(
    r"\b(?:stop (?:calling|this call|the call)|do not call|don't call|end (?:the|this) call|hang up)\b"
)
_POLITE = r"(?:(?:please|kindly|just|now)\s+)*"
_PREFIX = (
    rf"(?:{_POLITE}|(?:could|would|will|can)\s+you\s+{_POLITE}"
    rf"|i\s+(?:want|need|ask)\s+you\s+to\s+{_POLITE}"
    rf"|i(?:'d| would)\s+like\s+you\s+to\s+{_POLITE}"
    rf"|you\s+(?:must|need to|have to)\s+{_POLITE})"
)
_SUFFIX = r"(?:\s+(?:now|please|immediately|right now|for me|thanks|thank you))*"
_REASON = r"(?:\s+(?:because\b.*|(?:i|we)\s+(?:don't|do not|am|are|have|need|want)\b.*))?"
_WITHDRAWAL = (
    r"(?:stop calling|do not call|don't call)"
    r"(?:\s+(?:me|us|this number|my phone|my number|here))?(?:\s+(?:again|anymore|ever again))?"
)
_DIRECT_END = re.compile(rf"{_PREFIX}(?:hang up|(?:end|stop) (?:the|this) call){_SUFFIX}{_REASON}")
_DIRECT_WITHDRAWAL = re.compile(rf"{_PREFIX}{_WITHDRAWAL}\b")
_CONDITION = re.compile(r"(?:^|\bor\s+)(?:if|when|once|unless|until|whenever)\b")
_OPTION_CONTINUATION = re.compile(
    r"^(?:(?:if|when|once|unless|until|whenever)\b|or\s+(?:say|press|dial|select|choose|return)\b)"
)
_DIRECT_FAREWELL = re.compile(
    r"(?:(?:okay|ok|alright|thanks|thank you)\s+)*"
    r"(?P<farewell>goodbye|bye(?: bye)?|have a (?:good|nice|great) day)"
    r"(?:\s+(?:thanks|thank you))*"
)
_FAREWELL_SUPPORT = re.compile(r"thanks|thank you|talk to you later|see you later|take care")
_FAREWELL_CONTEXT = re.compile(
    r"\b(?:say|says|said|quote|quoted|story|topic|background|television|tv|radio|"
    r"menu|press|dial|select|choose|option|repeat)\b"
)


def _normalize(text):
    return " ".join(text.replace("’", "'").casefold().split())


def _clauses(text):
    # Commas do not establish independence: "if you are done, hang up" is one
    # instruction. ASR sentence boundaries also need their neighboring context.
    return [
        part.strip(" ,:") for part in re.split(r"[.!?;]+", _normalize(text)) if part.strip(" ,:")
    ]


def _command(clause):
    return re.sub(r"^(?:(?:okay|ok|alright|actually|thanks|thank you)[,\s]+)+", "", clause)


def _unfinished_option(clause):
    return not _STOP_PHRASE.search(clause) and bool(
        _CONDITION.search(clause)
        or re.search(r"\b(?:you (?:may|can)(?: now)?|feel free to)\s*$", clause)
    )


def recipient_requests_stop(text, *, preceding_text=""):
    """Only a complete direct command can authorize automatic teardown.

    Conditions, menu choices, negations and reported speech cannot match the
    command grammar. Preserve a condition even when ASR punctuates or splits it
    into a separate recipient turn. Explicit withdrawal still takes precedence.
    """
    clauses = _clauses(text)
    previous = _clauses(preceding_text)
    for index, clause in enumerate(clauses):
        command = _command(clause)
        # A condition on who is calling does not waive withdrawal of consent.
        withdrawal = re.sub(r"^if\b[^,]+,\s*", "", command)
        withdrawal_match = _DIRECT_WITHDRAWAL.match(withdrawal.replace(",", " "))
        if withdrawal_match:
            remainder = withdrawal.replace(",", " ")[withdrawal_match.end() :].strip()
            # Do not require a recipient to finish explaining why they want no
            # more calls. A phrase described as a menu label is not a command.
            if not re.match(r"(?:is|was|means|refers)\b", remainder):
                return True
        if not _DIRECT_END.fullmatch(command.replace(",", " ")):
            continue
        before = clauses[index - 1] if index else previous[-1] if previous else ""
        after = clauses[index + 1] if index + 1 < len(clauses) else ""
        if _unfinished_option(before) or _OPTION_CONTINUATION.search(after):
            continue
        return True
    return False


def recipient_requests_stop_in_dialog(dialog):
    turns = [entry["text"] for entry in dialog if entry.get("role") == "user"][-2:]
    return bool(turns) and recipient_requests_stop(
        turns[-1], preceding_text=turns[-2] if len(turns) > 1 else ""
    )


def recipient_quote_requests_stop(quote, dialog):
    turns = [entry["text"] for entry in dialog if entry.get("role") == "user"][-2:]
    # A short or paraphrased quote must not overrule known context. If a newer
    # direct request follows a menu, its completed transcript can authorize stop.
    if any(_STOP_PHRASE.search(_normalize(turn)) for turn in turns):
        return False
    return bool(quote) and recipient_requests_stop(quote, preceding_text=turns[-1] if turns else "")


def recipient_farewell_in_dialog(dialog, *, quote=None):
    """Recognize a direct closing, never a farewell mentioned in other speech.

    A model quote must match an observed farewell clause. This text check does
    not identify speakers; callers must also validate turn freshness and tool
    intent before accepting a farewell without a verified task outcome.
    """
    turns = [entry["text"] for entry in dialog if entry.get("role") == "user"][-2:]
    if not turns or (
        len(turns) > 1
        and (
            _FAREWELL_CONTEXT.search(_normalize(turns[-2]))
            or _unfinished_option(_normalize(turns[-2]))
        )
    ):
        return False

    def farewells(text):
        if not isinstance(text, str) or text.rstrip().endswith("?"):
            return set()
        found = set()
        for clause in _clauses(text):
            clause = " ".join(clause.replace(",", " ").split())
            match = _DIRECT_FAREWELL.fullmatch(clause)
            if match:
                found.add(match["farewell"])
            elif not _FAREWELL_SUPPORT.fullmatch(clause):
                return set()
        return found

    observed = farewells(turns[-1])
    if quote is None:
        return bool(observed)
    quoted = farewells(quote)
    return bool(quoted) and quoted <= observed
