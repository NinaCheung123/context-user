"""Per-session study measures, phase labels, and detector validation."""
from __future__ import annotations

import csv
import random
from collections import defaultdict
from datetime import date
from pathlib import Path

from .detect import Flags
from .sources import EDIT_TOOLS, ToolUse

SESSION_FIELDS = ["source", "session", "project", "start", "end", "minutes", "cold_start_min", "phase",
                  "turns", "corrections", "repeats", "friction_rate"]


def load_phases(path: Path | None) -> list[tuple[date, date, str]]:
    """phases.csv columns: start,end,phase (dates inclusive, e.g. 2026-10-19,2026-10-25,A)."""
    if not path or not path.exists():
        return []
    with path.open(newline="") as f:
        return [(date.fromisoformat(r["start"]), date.fromisoformat(r["end"]), r["phase"])
                for r in csv.DictReader(f)]


def phase_of(d: date | None, phases) -> str:
    if d is None:
        return ""
    return next((p for s, e, p in phases if s <= d <= e), "")


def first_edits(tools: list[ToolUse]) -> dict[str, object]:
    """Earliest file edit by the agent in each Claude Code session."""
    out: dict = {}
    for t in tools:
        if t.name in EDIT_TOOLS and t.ts and (t.session not in out or t.ts < out[t.session]):
            out[t.session] = t.ts
    return out


def sessions(flags: list[Flags], phases=(), tools: list[ToolUse] = ()) -> list[dict]:
    """One row per session.

    cold_start_min: minutes from the first prompt to the agent's first file
    edit (Claude Code only; blank if the session never edited a file).
    """
    edits = first_edits(tools)
    by: dict[tuple, list[Flags]] = defaultdict(list)
    for f in flags:
        by[(f.turn.source, f.turn.session)].append(f)
    rows = []
    for (source, sid), fs in by.items():
        times = [f.turn.ts for f in fs if f.turn.ts]
        start, end = (min(times), max(times)) if times else (None, None)
        n = len(fs)
        corr = sum(1 for f in fs if f.correction)
        rep = sum(1 for f in fs if f.repeat_of)
        edit = edits.get(sid) if source == "claude" else None
        cold = round((edit - start).total_seconds() / 60, 1) if edit and start and edit >= start else ""
        rows.append({
            "source": source, "session": sid, "project": fs[0].turn.project,
            "start": start.isoformat() if start else "", "end": end.isoformat() if end else "",
            "minutes": round((end - start).total_seconds() / 60, 1) if start else "",
            "cold_start_min": cold,
            "phase": phase_of(start.date() if start else None, phases),
            "turns": n, "corrections": corr, "repeats": rep,
            "friction_rate": round((corr + rep) / n, 3) if n else 0,
        })
    rows.sort(key=lambda r: r["start"])
    return rows


def summarize(rows: list[dict]) -> dict[str, dict]:
    by: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by[r["phase"] or "-"].append(r)
    out = {}
    for phase, rs in sorted(by.items()):
        turns = sum(r["turns"] for r in rs)
        colds = sorted(r["cold_start_min"] for r in rs if r["cold_start_min"] != "")
        out[phase] = {
            "sessions": len(rs), "turns": turns,
            "corrections_per_100_turns": round(100 * sum(r["corrections"] for r in rs) / turns, 1) if turns else 0,
            "repeats_per_100_turns": round(100 * sum(r["repeats"] for r in rs) / turns, 1) if turns else 0,
            "median_cold_start_min": colds[len(colds) // 2] if colds else None,
        }
    return out


def write_csv(rows: list[dict], path: Path, fields=SESSION_FIELDS) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


# ------------------------------------------------------ detector validation

LABEL_FIELDS = ["id", "text", "det_correction", "det_repeat",
                "llm_correction", "llm_repeat", "human_correction", "human_repeat"]


def label_sample(flags: list[Flags], n: int = 100, seed: int = 7) -> list[dict]:
    """Half flagged, half unflagged turns, shuffled, for labelling.

    Workflow: an LLM fills llm_* (1/0) for every row; you fill human_* only
    for the rows `cu score` lists as disagreements. The final label is the
    human one where present, otherwise the LLM's.
    """
    rnd = random.Random(seed)
    flagged = [f for f in flags if f.correction or f.repeat_of]
    clean = [f for f in flags if not (f.correction or f.repeat_of)]
    pick = rnd.sample(flagged, min(n // 2, len(flagged)))
    pick += rnd.sample(clean, min(n - len(pick), len(clean)))
    rnd.shuffle(pick)
    return [{"id": f.turn.id, "text": " ".join(f.turn.text.split())[:500],
             "det_correction": int(bool(f.correction)), "det_repeat": int(bool(f.repeat_of)),
             "llm_correction": "", "llm_repeat": "",
             "human_correction": "", "human_repeat": ""} for f in pick]


def _agreement(pairs: list[tuple[bool, bool]]) -> dict:
    """Precision / recall / Cohen's kappa of the first value against the second."""
    tp = sum(a and b for a, b in pairs); fp = sum(a and not b for a, b in pairs)
    fn = sum(b and not a for a, b in pairs); tn = sum(not a and not b for a, b in pairs)
    n = len(pairs)
    po = (tp + tn) / n if n else 0
    pe = ((tp + fp) * (tp + fn) + (fn + tn) * (fp + tn)) / n ** 2 if n else 0
    return {
        "n": n,
        "precision": round(tp / (tp + fp), 2) if tp + fp else None,
        "recall": round(tp / (tp + fn), 2) if tp + fn else None,
        "kappa": round((po - pe) / (1 - pe), 2) if n and pe < 1 else None,
    }


def score(path: Path) -> dict:
    """Score the detector against final labels, and list rows still to check.

    Final label = human if filled, else LLM. Rows where detector and LLM
    disagree and no human label exists are returned under "to_check".
    LLM-vs-human agreement on the rows you checked is also reported, so the
    paper can say how far the LLM labels can be trusted.

    Note: the sample over-represents flagged turns, so recall is on the
    sample, not the population; report it as such.
    """
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    out: dict = {"to_check": []}
    for kind in ("correction", "repeat"):
        det_vs_final, llm_vs_human = [], []
        for r in rows:
            det, llm, hum = (r[f"{who}_{kind}"].strip() for who in ("det", "llm", "human"))
            if hum == "" and llm != "" and llm != det:
                out["to_check"].append(f"{r['id']} ({kind})")
            final = hum or llm
            if final:
                det_vs_final.append((det == "1", final == "1"))
            if hum and llm:
                llm_vs_human.append((llm == "1", hum == "1"))
        out[kind] = {"detector_vs_final": _agreement(det_vs_final),
                     "llm_vs_human": _agreement(llm_vs_human)}
    return out
