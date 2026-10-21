#!/usr/bin/env python3
"""Offline sanity checks for the public Product Hunt kit and its PNG exports."""

from __future__ import annotations

import hashlib
import json
import re
import struct
import zlib
from pathlib import Path
from xml.etree import ElementTree


ASSETS = Path(__file__).resolve().parent
ROOT = ASSETS.parents[2]
EXPECTED = {
    "01-let-dots-call": (1270, 760),
    "02-brief-first": (1270, 760),
    "03-help-during-the-call": (1270, 760),
    "04-the-answer-back": (1270, 760),
    "05-your-setup": (1270, 760),
    "thumbnail": (240, 240),
    "social-card": (1200, 630),
}


def png_dimensions(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "PNG signature missing"
    offset, dimensions, image_data = 8, None, bytearray()
    ended = False
    while offset < len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        chunk = data[offset + 4 : offset + 8]
        body = data[offset + 8 : offset + 8 + length]
        crc = struct.unpack(">I", data[offset + 8 + length : offset + 12 + length])[0]
        assert zlib.crc32(chunk + body) & 0xFFFFFFFF == crc, "PNG CRC mismatch"
        if chunk == b"IHDR":
            dimensions = struct.unpack(">II", body[:8])
        if chunk == b"IDAT":
            image_data.extend(body)
        if chunk == b"IEND":
            ended = True
        offset += length + 12
    assert ended and dimensions is not None, "Incomplete PNG"
    assert zlib.decompress(image_data), "Empty pixel stream"
    return dimensions


def main() -> None:
    records = []
    for name, expected in EXPECTED.items():
        png = ASSETS / f"{name}.png"
        data = png.read_bytes()
        assert png_dimensions(data) == expected, f"Wrong dimensions: {name}"
        assert len(data) < 3_000_000, f"Image exceeds 3 MB: {name}"
        source = (ASSETS / f"{name}.svg").read_text()
        element = ElementTree.fromstring(source)
        assert (int(element.attrib["width"]), int(element.attrib["height"])) == expected
        if name.startswith(("01-", "02-", "04-")):
            assert "FICTIONAL" in source, f"Fictional example label missing: {name}"
        if name[:2].isdigit():
            assert "EXPERIMENTAL PREVIEW" in source, f"Preview label missing: {name}"
        records.append({
            "file": png.name,
            "width": expected[0],
            "height": expected[1],
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        })

    copy = (ROOT / "docs/launch/PRODUCT-HUNT-COPY.md").read_text()
    fields = {}
    for field, maximum in (("Name", None), ("Tagline", 60), ("Description", 260)):
        match = re.search(rf"\*\*{field}\*\*[^\n]*\n\n```text\n(.*?)\n```", copy, re.S)
        assert match, f"Missing listing field: {field}"
        value = match.group(1)
        assert "\n" not in value, f"Multiline listing field: {field}"
        assert maximum is None or len(value) <= maximum, f"{field} exceeds {maximum}"
        fields[field.lower()] = {"characters": len(value), "limit": maximum}
    assert "experimental preview" in copy.lower()
    print(json.dumps({
        "status": "passed",
        "gallery_count": sum(record["file"][:2].isdigit() for record in records),
        "total_png_count": len(records),
        "listing_fields": fields,
        "assets": records,
    }, indent=2))


if __name__ == "__main__":
    main()
