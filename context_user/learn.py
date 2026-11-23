"""Periodic automatic learning: `cu learn`, run by a hook or cron.

One round:
  1. classify new messages (corrections, repeats) and revised drafts with Claude
  2. write every reusable lesson into .context/ (one-off decisions and
     duplicates are skipped); these are the "auto" lessons in `cu effect`
  3. sync to Claude Code and Cursor
  4. append a line to .context/learn.log

Safety:
  * CU_LEARNING set   → exit at once (the classifier's own `claude -p` calls
                        set it, so a SessionEnd hook can never loop)
  * lock file          → never two rounds at the same time
  * study running      → on "none" / "generated" days only classify, never write
"""
from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timezone
from pathlib import Path

from . import history, lessons, llm, revisions, study, sync
from .detect import jaccard, tokens

LOCK = Path(".context/.learn.lock")
LOG = Path(".context/learn.log")
AUTO_LOG = Path(".context/auto_added.jsonl")
LOCK_STALE_S = 30 * 60
WRITE_SCOPES = {"global", "project", "unknown"}  # never "one-off"
MAX_PER_ROUND = 5      # the pilot wrote 25 lessons in one round: bloat
NEAR_DUPLICATE = 0.5   # Jaccard similarity to an existing line


def _existing_text(root: Path) -> str:
    ctx = root / ".context"
    files = list(ctx.glob("*.md")) + list((ctx / "skills").glob("*.md"))
    return "\n".join(p.read_text(encoding="utf-8") for p in files).lower()


def collect(llm_cache: dict, rev_cache: dict) -> list[dict]:
    """Reusable lessons from both classifiers: [{id, lesson, scope, source}]."""
    out = []
    for r in llm_cache.values():
        if r.get("lesson") and r.get("scope", "unknown") in WRITE_SCOPES:
            out.append({"id": lessons._id(r["lesson"]), "lesson": r["lesson"],
                        "scope": r.get("scope", "unknown"), "source": "conversation"})
    for r in rev_cache.values():
        for x in r.get("lessons", []):
            if x.get("lesson") and x.get("scope") in WRITE_SCOPES:
                out.append({"id": lessons._id(x["lesson"]), "lesson": x["lesson"],
                            "scope": x["scope"], "source": f"revision:{r.get('kind', '')}"})
    return out


def _existing_lines(root: Path) -> list[frozenset]:
    return [tokens(l) for l in _existing_text(root).splitlines() if l.strip().startswith(("-", "*"))]


def auto_add(root: Path, found: list[dict], limit: int = MAX_PER_ROUND) -> list[dict]:
    """Write up to `limit` lessons that are new: not added before, and not a
    near-duplicate of a line already in .context/. Global lessons go to
    LESSONS.md or a matching skill; project lessons to PROJECT.md."""
    log = root / AUTO_LOG
    done = {json.loads(l)["id"] for l in log.read_text(encoding="utf-8").splitlines() if l.strip()} \
        if log.exists() else set()
    existing = _existing_lines(root)
    added = []
    for f in found:
        if len(added) >= limit:
            break
        toks = tokens(f["lesson"])
        if f["id"] in done or any(jaccard(toks, e) >= NEAR_DUPLICATE for e in existing):
            continue
        home = lessons.place(root, f["lesson"])
        if f["scope"] == "project" and home == "global":
            home = "project"
            with (root / ".context/PROJECT.md").open("a", encoding="utf-8") as fh:
                fh.write(f"- {f['lesson']}\n")
        else:
            lessons.add(root, f["lesson"], home)
        added.append({**f, "home": home, "at": datetime.now(timezone.utc).isoformat()})
        done.add(f["id"])
        existing.append(toks)
    if added:
        with log.open("a", encoding="utf-8") as fh:
            fh.write("".join(json.dumps(a, ensure_ascii=False) + "\n" for a in added))
    return added


def writing_allowed(root: Path, today: date | None = None) -> tuple[bool, str]:
    try:
        cfg = study.load(root)
    except FileNotFoundError:
        return True, "no study"
    cond = study.condition_on(cfg, today or date.today())
    return cond == "context-user", f"study condition today: {cond or 'none scheduled'}"


def learn(root: Path, turns, revs, tools=(), model: str = "haiku", write: bool = True,
          runner=None, say=print) -> dict:
    """One learning round. `turns`/`revs` are the already-read messages and revisions."""
    if os.environ.get("CU_LEARNING"):
        return {"skipped": "nested call"}
    lock = root / LOCK
    if lock.exists() and time.time() - lock.stat().st_mtime < LOCK_STALE_S:
        return {"skipped": "another round is running"}
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(os.getpid()))
    try:
        quiet = lambda *_: None  # noqa: E731
        kw = {"runner": runner} if runner else {}
        llm_cache = llm.classify(root, turns, model, say=quiet, **kw)
        rev_cache = revisions.classify(root, revs, model, say=quiet, **kw) if revs else revisions.load_cache(root)
        allowed, why = writing_allowed(root)
        added: list[dict] = []
        synced = False
        if write and allowed:
            added = auto_add(root, collect(llm_cache, rev_cache))
            if added:
                history.snapshot(root, list(tools), channel_hint="auto")
                try:
                    sync.sync(root)
                    synced = True
                except FileExistsError as e:  # a hand-written CLAUDE.md: leave it, report it
                    why += f"; sync skipped: {e}"
        result = {"at": datetime.now(timezone.utc).isoformat(), "messages": len(turns), "revisions": len(revs),
                  "added": len(added), "synced": synced, "writing": bool(write and allowed), "note": why,
                  "lessons": [a["lesson"] for a in added]}
        with (root / LOG).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(result, ensure_ascii=False) + "\n")
        return result
    finally:
        lock.unlink(missing_ok=True)
