"""Did the teaching take effect? Classify every lesson into four cells.

                      mistake did NOT recur      mistake recurred
  skill loaded        effective                  ignored (loaded, not followed)
  skill not loaded    dormant (not needed yet)   routing failure (not found)

A *lesson* is a bullet line added to .context/ (from the history log).
*Loaded*: the lesson's file is always loaded (USER/PROJECT/LESSONS.md), or its
skill was invoked or read by Claude Code after the lesson was added. A load is
"auto" if the agent chose the skill itself, or "user" if the developer's
preceding prompt named the skill. Both count as loaded; a high share of "user"
loads means the router is not surfacing skills on its own.
*Recurred*: a later correction/repeat turn shares at least two content words
with the lesson. Both are heuristics; report them as such.
Load events are only observable for Claude Code, not Cursor.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime

from .detect import Flags, tokens
from .sources import ToolUse

ALWAYS_LOADED = {"USER.md", "PROJECT.md", "LESSONS.md"}
STOP = frozenset("the and use not for with you this that from are was were have has always never "
                 "don please should when what then than into only also just like make sure".split())
MIN_OVERLAP = 2


def content_tokens(text: str) -> frozenset:
    return tokens(text) - STOP


def lessons(events: list[dict]) -> list[dict]:
    """Bullet lines added to .context/, with when they were added and removed."""
    out: list[dict] = []
    for e in events:
        at = datetime.fromisoformat(e["at"])
        for line in e["removed"]:
            for l in out:
                if l["file"] == e["file"] and l["line"] == line and l["removed_at"] is None:
                    l["removed_at"] = at
                    break
        for a in e["added"]:
            empty_field = re.sub(r"[*_`]", "", a["text"]).rstrip().endswith(":")  # e.g. "- **Name:**"
            if a["text"].startswith(("- ", "* ")) and not empty_field:
                out.append({"file": e["file"], "line": a["text"], "text": a["text"][2:], "added_at": at,
                            "removed_at": None, "channel": e["channel"],
                            # "auto": written automatically by `cu classify --auto-add`;
                            # "proposed": matches an earlier `cu suggest` proposal the developer
                            # adopted; "own": the developer wrote it unprompted
                            "origin": "auto" if e["channel"] == "auto" else
                                      "proposed" if a.get("proposal") else "own"})
    return out


def skill_of(file: str) -> str | None:
    return file.removeprefix("skills/").removesuffix(".md") if file.startswith("skills/") else None


def _is_load(t: ToolUse, skill: str) -> bool:
    if t.name == "Skill":
        name = str(t.input.get("skill", ""))
        return name == skill or name.endswith(":" + skill)
    if t.name == "Read":
        p = str(t.input.get("file_path", "")).replace("\\", "/")
        return p.endswith(f"/.claude/skills/{skill}/SKILL.md") or p.endswith(f".context/skills/{skill}.md")
    return False


def load_counts(tools: list[ToolUse]) -> Counter:
    """Times each skill was loaded by Claude Code (any time)."""
    c: Counter = Counter()
    for t in tools:
        if t.name == "Skill":
            c[str(t.input.get("skill", "")).split(":")[-1]] += 1
        elif t.name == "Read":
            p = str(t.input.get("file_path", "")).replace("\\", "/")
            if "/.claude/skills/" in p and p.endswith("/SKILL.md"):
                c[p.split("/.claude/skills/")[1].split("/")[0]] += 1
            elif "/.context/skills/" in p:
                c[p.split("/.context/skills/")[1].removesuffix(".md")] += 1
    return c


def _mentions(text: str, skill: str) -> bool:
    text = text.lower()
    return skill in text or skill.replace("-", " ") in text


def load_trigger(t: ToolUse, skill: str, flags: list[Flags]) -> str:
    """"user" if the last prompt before this load in its session named the skill."""
    prior = [f.turn for f in flags
             if f.turn.session == t.session and f.turn.ts and t.ts and f.turn.ts <= t.ts]
    if prior and _mentions(max(prior, key=lambda x: x.ts).text, skill):
        return "user"
    return "auto"


def classify(lesson_list: list[dict], flags: list[Flags], tools: list[ToolUse],
             in_condition=lambda ts: True) -> list[dict]:
    """Put each lesson in one of the four cells.

    `in_condition(ts)` restricts evidence to times when Context-User was active,
    so recurrences during a "no context" block don't count against a lesson.
    """
    friction = [f for f in flags if (f.correction or f.repeat_of) and f.turn.ts]
    out = []
    for l in lesson_list:
        start, end = l["added_at"], l["removed_at"]

        def window(ts):
            return ts and ts > start and (end is None or ts <= end) and in_condition(ts)

        lt = content_tokens(l["text"])
        recurred = sum(1 for f in friction if window(f.turn.ts) and len(lt & content_tokens(f.turn.text)) >= MIN_OVERLAP)
        skill = skill_of(l["file"])
        loads_auto = loads_user = None
        if skill is None:
            loaded = l["file"] in ALWAYS_LOADED
        else:
            triggers = [load_trigger(t, skill, flags) for t in tools if window(t.ts) and _is_load(t, skill)]
            loads_auto, loads_user = triggers.count("auto"), triggers.count("user")
            loaded = bool(triggers)
        cell = ("effective" if loaded and not recurred else
                "ignored" if loaded else
                "routing_failure" if recurred else "dormant")
        out.append({**l, "skill": skill or "(always loaded)", "loads_auto": loads_auto, "loads_user": loads_user,
                    "recurrences": recurred, "cell": cell})
    return out


CELLS = ("effective", "ignored", "routing_failure", "dormant")


def table(rows: list[dict]) -> dict[str, int]:
    c = Counter(r["cell"] for r in rows)
    return {k: c.get(k, 0) for k in CELLS}


def table_by_origin(rows: list[dict]) -> dict[str, dict[str, int]]:
    """The four cells per origin: written automatically ("auto"), suggested and
    adopted by the developer ("proposed"), or self-written ("own").

    This is the direct test of whether automatically learned lessons work as
    well as the developer's own; either answer is a result.
    """
    return {o: table([r for r in rows if r["origin"] == o]) for o in ("auto", "proposed", "own")}
