"""UTF-8-safe context injection, adapted from OpenAI's MIT voice eval harness."""


def split_context_text(text: str, *, max_bytes: int = 400) -> list[str]:
    """Conservatively stay below 500 tokens per append without a tokenizer."""
    if max_bytes < 4:
        raise ValueError("max_bytes must fit a complete UTF-8 character")
    if not text.strip():
        return []
    chunks, current, used = [], [], 0
    for character in text:
        size = len(character.encode("utf-8"))
        if current and used + size > max_bytes:
            chunks.append("".join(current))
            current, used = [], 0
        current.append(character)
        used += size
    if current:
        chunks.append("".join(current))
    return chunks
