"""Static, packaged skill snapshots for the ChatGPT plugin importer."""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
from copy import deepcopy
from pathlib import Path

import yaml

EXTENSION = "io.modelcontextprotocol/skills"
SKILL_URI = "skill://dots-phone/dots-phone/SKILL.md"


class SkillCatalog:
    def __init__(self, directory: Path | None = None):
        if directory is None:
            package = Path(__file__).parent / "_plugin"
            if not package.is_dir():
                package = Path(__file__).resolve().parents[1] / "plugins" / "dots-phone"
            directory = package / "skills" / "dots-phone"
        if directory.is_symlink():
            raise ValueError("skill directory must not be a symlink")
        root = directory.resolve(strict=True)
        self.contents = {}
        resources = []
        total = 0
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise ValueError("skill resources must not be symlinks")
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            raw = path.read_bytes()
            total += len(raw)
            limit = 256 * 1024 if relative == "SKILL.md" else 1024 * 1024
            if len(raw) > limit or total > 5 * 1024 * 1024 or len(resources) >= 100:
                raise ValueError("packaged skill exceeds import limits")
            uri = "skill://dots-phone/dots-phone/" + relative
            mime = mimetypes.guess_type(relative)[0] or "application/octet-stream"
            content = {"uri": uri, "mimeType": mime}
            try:
                content["text"] = raw.decode("utf-8")
            except UnicodeDecodeError:
                content["blob"] = base64.b64encode(raw).decode("ascii")
            self.contents[uri] = content
            resources.append({"uri": uri, "digest": "sha256:" + hashlib.sha256(raw).hexdigest()})
        main = self.contents.get(SKILL_URI, {}).get("text", "")
        if not main.startswith("---\n") or "\n---\n" not in main[4:]:
            raise ValueError("skill requires YAML front matter")
        frontmatter = yaml.safe_load(main[4:].split("\n---\n", 1)[0])
        if (
            not isinstance(frontmatter, dict)
            or frontmatter.get("name") != "dots-phone"
            or not isinstance(frontmatter.get("description"), str)
            or not frontmatter["description"].strip()
        ):
            raise ValueError("invalid skill front matter")
        json.dumps(frontmatter)
        self.skill = {"uri": SKILL_URI, "frontmatter": frontmatter, "resources": resources}

    def list(self, cursor=None):
        if cursor is not None:
            raise ValueError("unknown skill cursor")
        return {"skills": [deepcopy(self.skill)]}

    def get(self, uri):
        if uri != SKILL_URI:
            raise ValueError("unknown skill")
        return {"skill": deepcopy(self.skill)}

    def read(self, uri):
        if uri not in self.contents:
            raise ValueError("unknown skill resource")
        return {"contents": [deepcopy(self.contents[uri])]}

    def workflow(self):
        return {
            "version": self.skill["frontmatter"]["metadata"]["version"],
            "origin": "static_packaged_phone_skill",
            "skill": deepcopy(self.skill),
            "contents": deepcopy(list(self.contents.values())),
        }
