"""Turn flagged friction into lesson proposals. The developer decides how to use them.

Nothing is written automatically and nothing has to be accepted or rejected:
the developer teaches however they like (editing files, asking the agent,
`cu add`). Proposals are stored so that the history log can later tell
which teaching came from them and how much it was rewritten.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from . import skills as sk
from .detect import Flags, jaccard, own_words, tokens

LESSONS = Path(".context/LESSONS.md")


def _id(text: str) -> str:
    return hashlib.sha1(text.lower().encode()).hexdigest()[:8]


def candidates(flags: list[Flags], min_count: int = 1) -> list[dict]:
    """Group similar friction turns; the most frequent groups come first."""
    groups: list[dict] = []
    for f in flags:
        if not (f.correction or f.repeat_of) or f.turn.kind == "interrupt":
            continue  # an interruption is friction, but carries no lesson text
        quote = " ".join(own_words(f.turn.text).split())[:240]
        text = f.lesson or quote  # an LLM-drafted rule if there is one, else the quote
        toks = tokens(f.lesson) if f.lesson else f.tokens
        for g in groups:
            if jaccard(g["tokens"], toks) >= 0.4:
                g["count"] += 1
                g["examples"].append(quote)
                break
        else:
            groups.append({"text": text, "tokens": toks, "count": 1, "examples": [quote],
                           "kind": "correction" if f.correction else "repeat"})
    groups = [g for g in groups if g["count"] >= min_count]
    groups.sort(key=lambda g: -g["count"])
    for g in groups:
        g["id"] = _id(g["text"])
        g["examples"] = g["examples"][:5]
        del g["tokens"]
    return groups


def place(root: Path, text: str) -> str:
    """Where a lesson most likely belongs: a skill name or 'global'."""
    s = sk.suggest(text, sk.load(root))
    return s.name if s else "global"


def add(root: Path, text: str, skill: str | None = None) -> Path:
    """Append a lesson as a global rule or as a known pitfall of a skill."""
    line = f"- {text.strip()}"
    if skill and skill != "global":
        path = root / sk.SKILLS_DIR / f"{sk.slug(skill)}.md"
        if not path.exists():
            raise FileNotFoundError(f"No skill '{skill}'. Create it first or omit --skill.")
        sk.add_pitfall(path, line)
        return path
    path = root / LESSONS
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    return path
