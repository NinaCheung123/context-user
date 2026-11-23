"""Record how teaching material changes, however the developer chooses to teach.

We don't ask the developer to accept or reject anything. We snapshot the
Markdown files in .context/ and record every change as a *teaching event*:
which file, which lines were added or removed, and when. Each event is then
attributed:

  channel   "conversation" if a Claude Code agent edited that file in the
            interval, "cli" for `cu add`, "initial" for the first snapshot,
            otherwise "manual"
  proposal  the earlier `cu suggest` proposal the added text most resembles,
            with a similarity in [0, 1] (1 = adopted as-is; low = rewritten)

Snapshots are taken automatically by every `cu` command; `cu snapshot` can
also be run from a hook or cron for finer timing.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .detect import jaccard, tokens
from .sources import EDIT_TOOLS, ToolUse

CTX = Path(".context")
STATE = CTX / ".snapshot.json"
EVENTS = CTX / "history.jsonl"
PROPOSALS = CTX / "proposals.jsonl"
LINK_THRESHOLD = 0.25  # minimum similarity to link an added line to a proposal


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _lines(path: Path) -> list[str]:
    """Meaningful lines: non-empty, not pure comments or front-matter fences."""
    out = []
    text = re.sub(r"<!--.*?-->", "", path.read_text(encoding="utf-8"), flags=re.S)  # hints and examples
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line == "---" or (line.startswith("<!--") and line.endswith("-->")):
            continue
        out.append(line)
    return out


def _tracked(root: Path) -> dict[str, list[str]]:
    ctx = root / CTX
    files = sorted(ctx.glob("*.md")) + sorted((ctx / "skills").glob("*.md"))
    return {p.relative_to(ctx).as_posix(): _lines(p) for p in files}


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _append_jsonl(path: Path, rows: list[dict]) -> None:
    if rows:
        with path.open("a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _diff(old: list[str], new: list[str]) -> tuple[list[str], list[str]]:
    """Multiset difference, so a duplicated line still counts once per copy."""
    remaining = list(old)
    added = []
    for line in new:
        if line in remaining:
            remaining.remove(line)
        else:
            added.append(line)
    return added, remaining


def snapshot(root: Path, tools: list[ToolUse] = (), channel_hint: str | None = None,
             at: datetime | None = None) -> list[dict]:
    """Compare .context/ with the last snapshot and record new teaching events."""
    if not (root / CTX).exists():
        return []
    at = at or _now()
    state_path = root / STATE
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else None
    prev_at = datetime.fromisoformat(state["at"]) if state else None
    prev_files = state["files"] if state else {}
    current = _tracked(root)

    proposals = _read_jsonl(root / PROPOSALS)
    events = []
    for rel in sorted(set(prev_files) | set(current)):
        added, removed = _diff(prev_files.get(rel, []), current.get(rel, []))
        if not added and not removed:
            continue
        if prev_at is None:
            channel = "initial"
        elif channel_hint:
            channel = channel_hint
        else:
            edited = any(t.name in EDIT_TOOLS and t.ts and prev_at < t.ts <= at
                         and str(t.input.get("file_path", "")).replace("\\", "/").endswith(f".context/{rel}")
                         for t in tools)
            channel = "conversation" if edited else "manual"
        events.append({
            "at": at.isoformat(), "since": prev_at.isoformat() if prev_at else None,
            "file": rel, "channel": channel, "removed": removed,
            "added": [dict(text=line, **_link(line, proposals, at)) for line in added],
        })
    _append_jsonl(root / EVENTS, events)
    state_path.write_text(json.dumps({"at": at.isoformat(), "files": current}, ensure_ascii=False),
                          encoding="utf-8")
    return events


def _link(line: str, proposals: list[dict], at: datetime) -> dict:
    """The most similar earlier proposal, if any is similar enough."""
    t = tokens(line)
    best, score = None, 0.0
    for p in proposals:
        if datetime.fromisoformat(p["at"]) > at:
            continue
        s = max(jaccard(t, tokens(x)) for x in [p["text"], *p.get("examples", [])])
        if s > score:
            best, score = p["id"], s
    if score >= LINK_THRESHOLD:
        return {"proposal": best, "similarity": round(score, 2)}
    return {"proposal": None, "similarity": None}


def record_proposals(root: Path, cands: list[dict], at: datetime | None = None) -> list[dict]:
    """Store new `cu suggest` proposals so later edits can be linked to them."""
    path = root / PROPOSALS
    known = {p["id"] for p in _read_jsonl(path)}
    at = at or _now()
    new = [dict(c, at=at.isoformat()) for c in cands if c["id"] not in known]
    _append_jsonl(path, new)
    return new


def events(root: Path) -> list[dict]:
    return _read_jsonl(root / EVENTS)


def summary(evts: list[dict]) -> dict:
    """Teaching per ISO week and channel, and how proposals were adopted."""
    weeks: dict[str, dict[str, int]] = {}
    sims = []
    linked = total = 0
    for e in evts:
        if e["channel"] == "initial":
            continue
        week = datetime.fromisoformat(e["at"]).strftime("%G-W%V")
        w = weeks.setdefault(week, {})
        w[e["channel"]] = w.get(e["channel"], 0) + len(e["added"])
        w["removed"] = w.get("removed", 0) + len(e["removed"])
        for a in e["added"]:
            total += 1
            if a["proposal"]:
                linked += 1
                sims.append(a["similarity"])
    return {
        "lines_added_by_week": weeks,
        "added_lines": total,
        "from_proposals": linked,
        "mean_similarity_to_proposal": round(sum(sims) / len(sims), 2) if sims else None,
    }
