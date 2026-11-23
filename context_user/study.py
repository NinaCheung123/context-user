"""Guided study mode, so anyone can run the same protocol and share results.

  cu study init      make a personal schedule of rotating condition blocks
  cu study today     show today's condition and switch the project to it
  cu study export    write an anonymized summary to send back (numbers only)

Conditions:
  none        no persistent context: generated files are removed
  generated   one-shot LLM context: the developer's own `/init` output, saved
              once and restored in every later "generated" block
  context-user  the accumulated .context/ synced to all tools

The block order is shuffled per participant (seeded by their pseudonym) to
balance order effects across people.
"""
from __future__ import annotations

import hashlib
import json
import random
import shutil
from datetime import date, timedelta
from pathlib import Path

from . import effect, history, metrics, sync

STUDY = Path(".context/study.json")
SAVED_GENERATED = Path(".context/generated/CLAUDE.md")
CONDITIONS = ("none", "generated", "context-user")
EXPORT_VERSION = 1


def _hash(text: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{text}".encode()).hexdigest()[:10]


def plan(start: date, weeks: int = 4, block_days: int = 2, seed: str = "") -> list[dict]:
    """Rotating blocks of working days (Mon–Fri); each cycle uses all conditions once."""
    rnd = random.Random(seed)
    days = []
    d = start
    while len(days) < weeks * 5:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    blocks, cycle = [], []
    for i in range(0, len(days), block_days):
        if not cycle:
            cycle = list(CONDITIONS)
            rnd.shuffle(cycle)
        chunk = days[i:i + block_days]
        blocks.append({"start": chunk[0].isoformat(), "end": chunk[-1].isoformat(), "phase": cycle.pop(0)})
    return blocks


def init(root: Path, participant: str, start: date, weeks: int = 4, block_days: int = 2) -> dict:
    cfg = {"participant": participant, "salt": _hash(participant, "cu-salt"),
           "blocks": plan(start, weeks, block_days, seed=participant)}
    path = root / STUDY
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    # The same schedule as a phases.csv, for `cu metrics --phases`.
    metrics.write_csv(cfg["blocks"], root / ".context/phases.csv", ["start", "end", "phase"])
    return cfg


def load(root: Path) -> dict:
    path = root / STUDY
    if not path.exists():
        raise FileNotFoundError("No study yet. Run `cu study init --participant <pseudonym>` first.")
    return json.loads(path.read_text(encoding="utf-8"))


def condition_on(cfg: dict, day: date) -> str | None:
    for b in cfg["blocks"]:
        if date.fromisoformat(b["start"]) <= day <= date.fromisoformat(b["end"]):
            return b["phase"]
    return None


def apply(root: Path, condition: str) -> list[str]:
    """Switch the project's agent context to `condition`. Returns messages to show."""
    claude = root / "CLAUDE.md"
    saved = root / SAVED_GENERATED
    if condition == "none":
        sync.clean(root)
        _remove_generated(root)
        msgs = ["No persistent context today: work as you would without any CLAUDE.md or rules."]
        if claude.exists():
            msgs.append("Warning: your own CLAUDE.md is still here. Move its content into .context/PROJECT.md "
                        "and delete it, or this block is not a 'no context' condition.")
        return msgs
    if condition == "context-user":
        _remove_generated(root)
        sync.sync(root)  # never forced: a hand-written CLAUDE.md is not overwritten
        return ["Context-User is active: teach as usual (edit .context/, ask your agent, `cu add`), then `cu sync`."]
    # generated
    sync.clean(root)
    if not saved.exists() and claude.exists() and sync.MARKER not in claude.read_text(encoding="utf-8"):
        saved.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(claude, saved)  # first time: capture the developer's /init output
    if not saved.exists():
        return ["Generated-context block, first time: run `/init` in Claude Code in this project,",
                "then run `cu study today` again to save it for later blocks. Do not edit it afterwards."]
    shutil.copy2(saved, claude)
    rule = root / ".cursor/rules/generated.mdc"
    rule.parent.mkdir(parents=True, exist_ok=True)
    rule.write_text("---\ndescription: One-shot generated context\nalwaysApply: true\n---\n"
                    + saved.read_text(encoding="utf-8"), encoding="utf-8")
    return ["One-shot generated context is active (your saved /init output). Do not edit it during the study."]


def _remove_generated(root: Path) -> None:
    claude = root / "CLAUDE.md"
    saved = root / SAVED_GENERATED
    if saved.exists() and claude.exists() and claude.read_text(encoding="utf-8") == saved.read_text(encoding="utf-8"):
        claude.unlink()
    rule = root / ".cursor/rules/generated.mdc"
    if rule.exists():
        rule.unlink()


def export(root: Path, flags, tools, detector: str = "rules") -> dict:
    """Numbers only: no prompts, no lesson text, no file or project names.

    Session ids, projects and skill names are salted hashes, so rows can be
    told apart without revealing what they were.
    """
    cfg = load(root)
    salt = cfg["salt"]
    phases = [(date.fromisoformat(b["start"]), date.fromisoformat(b["end"]), b["phase"]) for b in cfg["blocks"]]
    sessions = metrics.sessions(flags, phases, tools)
    sess_rows = [{
        "session": _hash(r["session"], salt), "project": _hash(r["project"], salt), "source": r["source"],
        "date": r["start"][:10], "phase": r["phase"], "minutes": r["minutes"], "cold_start_min": r["cold_start_min"],
        "turns": r["turns"], "corrections": r["corrections"], "repeats": r["repeats"],
    } for r in sessions]

    def in_context_user(ts):
        return metrics.phase_of(ts.date(), phases) == "context-user"

    lesson_rows = effect.classify(effect.lessons(history.events(root)), flags, tools, in_context_user)
    lessons_out = [{
        "lesson": _hash(r["line"], salt), "skill": _hash(r["skill"], salt), "origin": r["origin"],
        "channel": r["channel"], "added": r["added_at"].date().isoformat(),
        "removed": r["removed_at"].date().isoformat() if r["removed_at"] else None,
        "loads_auto": r["loads_auto"], "loads_user": r["loads_user"],
        "recurrences": r["recurrences"], "cell": r["cell"],
    } for r in lesson_rows]
    return {
        "version": EXPORT_VERSION,
        "detector": detector,
        "participant": _hash(cfg["participant"], "cu-participant"),
        "schedule": [{"start": b["start"], "end": b["end"], "phase": b["phase"]} for b in cfg["blocks"]],
        "summary_by_condition": metrics.summarize(sess_rows),
        "effect_by_origin": effect.table_by_origin(lesson_rows),
        "teaching": history.summary(history.events(root)),
        "sessions": sess_rows,
        "lessons": lessons_out,
    }
