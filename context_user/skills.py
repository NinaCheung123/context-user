"""Skills: reusable know-how the agent reads on demand.

One Markdown file per skill: a goal, acceptance criteria, resources and
boundaries, and *known pitfalls*. Pitfalls are never guessed in advance: they
are harvested from real session friction and approved by the developer.

Source of truth: .context/skills/<name>.md
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .detect import Flags, jaccard, tokens

SKILLS_DIR = Path(".context/skills")
PITFALLS = "## Known pitfalls"

SKILL_TEMPLATE = """---
name: {name}
description: {description}
---
# {title}

## Goal
<!-- One sentence: what this achieves. Describe the end state, not the steps. -->

## Acceptance criteria
<!-- Testable: an agent with no other context could check it is done. -->

## Resources & boundaries
<!-- Tools, files, commands it may use; what it must never do. -->

{pitfalls}
<!-- Only real failures. Never guess pitfalls in advance. -->
"""


@dataclass
class Skill:
    name: str
    description: str
    path: Path
    body: str  # everything after the front matter

    @property
    def tokens(self) -> frozenset:
        return tokens(f"{self.name.replace('-', ' ')} {self.description} {self.body}")


def slug(text: str) -> str:
    """Claude Code skill names: lowercase letters, digits and hyphens."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:64] or "skill"


def parse(path: Path) -> Skill:
    text = path.read_text(encoding="utf-8")
    meta, body = {}, text
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
        body = m.group(2)
    return Skill(slug(meta.get("name", path.stem)), meta.get("description", ""), path, body.strip())


def load(root: Path) -> list[Skill]:
    d = root / SKILLS_DIR
    return [parse(p) for p in sorted(d.glob("*.md"))] if d.exists() else []


def create(root: Path, name: str, description: str, title: str | None = None) -> Path:
    name = slug(name)
    path = root / SKILLS_DIR / f"{name}.md"
    if path.exists():
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(SKILL_TEMPLATE.format(name=name, description=description.replace("\n", " "),
                                          title=title or name.replace("-", " ").capitalize(),
                                          pitfalls=PITFALLS), encoding="utf-8")
    return path


def add_pitfall(path: Path, line: str) -> None:
    """Append a bullet at the end of the Known pitfalls section (creating it if needed)."""
    lines = path.read_text(encoding="utf-8").rstrip("\n").split("\n")
    start = next((i for i, l in enumerate(lines) if l.strip().lower() == PITFALLS.lower()), None)
    if start is None:
        lines += ["", PITFALLS, line]
    else:
        end = next((i for i in range(start + 1, len(lines)) if re.match(r"^#{1,2} ", lines[i])), len(lines))
        while end > start + 1 and not lines[end - 1].strip():
            end -= 1
        lines.insert(end, line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def suggest(text: str, skills: list[Skill]) -> Skill | None:
    """The skill a lesson most likely belongs to, or None if nothing fits."""
    t = tokens(text)
    best = max(skills, key=lambda s: len(t & s.tokens), default=None)
    return best if best and len(t & best.tokens) >= 2 else None


def recurring_tasks(flags: list[Flags], skills: list[Skill], min_sessions: int = 3) -> list[dict]:
    """Kinds of task you start again and again, not yet covered by a skill.

    We cluster each session's opening prompt *and* any instruction flagged as
    a repeat: follow-ups like "check X's style and rewrite this" usually come
    mid-session, not as the first message.
    """
    repeated = {f.repeat_of for f in flags if f.repeat_of}  # first occurrences of later repeats
    groups: list[dict] = []
    for f in flags:
        if not (f.turn.index == 0 or f.repeat_of or f.turn.id in repeated) or len(f.tokens) < 4:
            continue
        for g in groups:
            if jaccard(g["tokens"], f.tokens) >= 0.3:
                g["sessions"].add(f.turn.session)
                g["examples"].append(" ".join(f.turn.text.split())[:200])
                break
        else:
            groups.append({"tokens": f.tokens, "sessions": {f.turn.session},
                           "examples": [" ".join(f.turn.text.split())[:200]]})
    out = []
    for g in groups:
        if len(g["sessions"]) < min_sessions:
            continue
        if any(jaccard(g["tokens"], s.tokens) >= 0.2 for s in skills):
            continue
        out.append({"sessions": len(g["sessions"]), "examples": g["examples"][:5]})
    out.sort(key=lambda g: -g["sessions"])
    return out
