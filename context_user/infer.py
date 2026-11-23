"""Infer skills from a user's past interactions, have them rated, apply them, compare.

A *skill* is a reusable instruction for a recurring kind of task or a
recurring quality the user wants, which the user would otherwise have to
restate to refine the AI's output. It has three parts:
  when      the situations it applies to
  what      the instructions
  evidence  the past messages it was inferred from

Pipeline
  1. refinements()  messages in which the user refines an earlier answer
                    (from `cu classify` labels; keyword rules as fallback)
  2. infer()        group refinements that recur across conversations into
                    skills (Claude, via the user's own CLI; or a simple
                    word-overlap grouping without an LLM)
  3. write()        .context/inferred/skills.json (full), skills.md (with
                    quotes, stays local), ratings.csv (to fill in and share:
                    no quotes from conversations)
  4. rate()         a short survey per skill: value 1–5 and why
  5. apply()        turn well-rated skills into .context/skills/<name>.md
  6. compare()      before/after: repeated refinements per 100 messages
"""
from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

from . import skills as sk
from .detect import Flags, jaccard, own_words, tokens

OUT = Path(".context/inferred")
MAX_INPUT = 400  # refinements sent to the synthesis step

PROMPT = """{marker} Below are messages in which one user refined an AI assistant's output (asked it
to change or improve an answer). Each has an id, a conversation id, the user's words, and a drafted rule.

Infer up to {n} SKILLS from them. A skill is a reusable instruction for a recurring kind of task or a
recurring quality the user wants, which the user would otherwise have to restate. Rules:
- Only infer a skill if refinements from at least {min_conv} different conversations support it.
- Ignore one-off decisions about a single document or task.
- Merge refinements that ask for the same thing in different words.
- Order skills by how many conversations support them.

Return ONLY a JSON array:
[{{"name": "short-kebab-name", "title": "short human title", "when": "when it applies",
   "instructions": ["imperative instruction", ...], "domain": "writing|coding|research|other",
   "evidence": ["<id>", ...]}}]

Refinements:
{items}"""

RATING_FIELDS = ["skill", "title", "domain", "conversations", "rating_1_to_5", "why"]


def refinements(flags: list[Flags], labels: dict | None = None) -> list[dict]:
    """Messages that refine an earlier answer, with the drafted rule if any."""
    from .llm import key
    labels = labels or {}
    out = []
    for f in flags:
        lab = labels.get(key(f.turn))
        is_ref = bool(lab and (lab.get("refine") or lab.get("correction"))) or (
            not lab and bool(f.correction) and f.correction != "interrupt")
        if not is_ref or f.turn.kind == "interrupt":
            continue
        out.append({"id": f.turn.id, "conversation": f"{f.turn.source}:{f.turn.session}",
                    "source": f.turn.source, "ts": f.turn.ts.isoformat() if f.turn.ts else None,
                    "text": " ".join(own_words(f.turn.text).split())[:300],
                    "rule": (lab or {}).get("lesson", "")})
    return out


def _validate(raw: list[dict], refs: list[dict], n: int, min_conv: int) -> list[dict]:
    by_id = {r["id"]: r for r in refs}
    skills = []
    for s in raw:
        ev = [e for e in s.get("evidence", []) if e in by_id]
        convs = {by_id[e]["conversation"] for e in ev}
        if len(convs) < min_conv or not s.get("instructions"):
            continue
        skills.append({"name": sk.slug(s.get("name") or s.get("title", "skill")),
                       "title": s.get("title", ""), "when": s.get("when", ""),
                       "instructions": [str(i) for i in s["instructions"]],
                       "domain": s.get("domain", "other"), "evidence": ev,
                       "conversations": len(convs), "refinements": len(ev)})
    skills.sort(key=lambda s: (-s["conversations"], -s["refinements"]))
    seen, unique = set(), []
    for s in skills:  # keep names unique
        while s["name"] in seen:
            s["name"] += "-2"
        seen.add(s["name"])
        unique.append(s)
    return unique[:n]


def infer(refs: list[dict], n: int = 10, min_conv: int = 2, model: str = "sonnet",
          runner=None) -> list[dict]:
    """Group recurring refinements into skills."""
    if not refs:
        return []
    if runner is False:  # no LLM: word-overlap grouping
        return _infer_simple(refs, n, min_conv)
    from .llm import _claude, _parse
    from .sources import CLASSIFIER_MARKER
    items = json.dumps([{"id": r["id"], "conversation": r["conversation"], "user": r["text"][:200],
                         "rule": r["rule"]} for r in refs[-MAX_INPUT:]], ensure_ascii=False, indent=0)
    raw = _parse((runner or _claude)(PROMPT.format(marker=CLASSIFIER_MARKER, n=n, min_conv=min_conv,
                                                   items=items), model))
    return _validate(raw, refs, n, min_conv)


def _infer_simple(refs: list[dict], n: int, min_conv: int) -> list[dict]:
    groups: list[dict] = []
    for r in refs:
        toks = tokens(r["rule"] or r["text"])
        for g in groups:
            if jaccard(g["tokens"], toks) >= 0.3:
                g["refs"].append(r)
                break
        else:
            groups.append({"tokens": toks, "refs": [r]})
    raw = [{"name": (g["refs"][0]["rule"] or g["refs"][0]["text"])[:40], "title": g["refs"][0]["rule"] or g["refs"][0]["text"][:80],
            "when": "", "instructions": [g["refs"][0]["rule"] or g["refs"][0]["text"]], "domain": "other",
            "evidence": [r["id"] for r in g["refs"]]} for g in groups]
    return _validate(raw, refs, n, min_conv)


def write(root: Path, skill_list: list[dict], refs: list[dict]) -> dict[str, Path]:
    out = root / OUT
    out.mkdir(parents=True, exist_ok=True)
    by_id = {r["id"]: r for r in refs}
    (out / "skills.json").write_text(json.dumps({"inferred_at": datetime.now().isoformat(timespec="seconds"),
                                                 "skills": skill_list}, ensure_ascii=False, indent=2), encoding="utf-8")
    md = ["# Skills inferred from your history", "",
          "Each skill was inferred from refinements you made in several conversations. "
          "The quotes stay on your machine; only ratings.csv is meant to be shared.", ""]
    for i, s in enumerate(skill_list, 1):
        md += [f"## {i}. {s['title']}  (`{s['name']}`, {s['domain']})", "",
               f"**When:** {s['when']}", "", "**What to do:**"] + [f"- {x}" for x in s["instructions"]] + [
               "", f"**Evidence:** {s['refinements']} refinements in {s['conversations']} conversations, e.g.:"]
        md += [f"> {by_id[e]['text'][:160]}" for e in s["evidence"][:3] if e in by_id] + [""]
    (out / "skills.md").write_text("\n".join(md), encoding="utf-8")
    with (out / "ratings.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=RATING_FIELDS)
        w.writeheader()
        for s in skill_list:
            w.writerow({"skill": s["name"], "title": s["title"], "domain": s["domain"],
                        "conversations": s["conversations"], "rating_1_to_5": "", "why": ""})
    return {"json": out / "skills.json", "md": out / "skills.md", "ratings": out / "ratings.csv"}


def load(root: Path) -> list[dict]:
    p = root / OUT / "skills.json"
    return json.loads(p.read_text(encoding="utf-8"))["skills"] if p.exists() else []


def rate(root: Path, ask=input, say=print) -> Path:
    """Short survey: for each inferred skill, value 1–5 and a reason."""
    skill_list = load(root)
    path = root / OUT / "ratings.csv"
    rows = []
    say("Rate each skill: how valuable would it be to have it applied automatically?")
    say("1 = not valuable … 5 = very valuable. Press Enter to skip.\n")
    for i, s in enumerate(skill_list, 1):
        say(f"[{i}/{len(skill_list)}] {s['title']}  ({s['domain']}, from {s['conversations']} conversations)")
        say(f"   When: {s['when']}")
        for x in s["instructions"]:
            say(f"   - {x}")
        r = ask("   Value 1-5: ").strip()
        while r and r not in "12345":
            r = ask("   Please type a number from 1 to 5, or Enter to skip: ").strip()
        why = ask("   Why? ").strip() if r else ""
        rows.append({"skill": s["name"], "title": s["title"], "domain": s["domain"],
                     "conversations": s["conversations"], "rating_1_to_5": r, "why": why})
        say("")
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=RATING_FIELDS)
        w.writeheader()
        w.writerows(rows)
    return path


def ratings(root: Path) -> dict[str, dict]:
    p = root / OUT / "ratings.csv"
    if not p.exists():
        return {}
    with p.open(newline="", encoding="utf-8") as f:
        return {r["skill"]: r for r in csv.DictReader(f)}


def summary(rows: list[dict]) -> dict:
    """Ratings per domain: n, mean, share rated 4 or 5."""
    by: dict[str, list[int]] = {}
    for r in rows:
        if r.get("rating_1_to_5", "").strip() in list("12345"):
            v = int(r["rating_1_to_5"])
            by.setdefault("all", []).append(v)
            by.setdefault(r.get("domain") or "other", []).append(v)
    return {d: {"n": len(v), "mean": round(sum(v) / len(v), 2), "share_4_or_5": round(sum(x >= 4 for x in v) / len(v), 2)}
            for d, v in by.items()}


def apply(root: Path, min_rating: int = 4) -> list[Path]:
    """Write skills rated >= min_rating into .context/skills/ (then `cu sync`)."""
    rated = ratings(root)
    written = []
    for s in load(root):
        r = rated.get(s["name"], {}).get("rating_1_to_5", "").strip()
        if not (r.isdigit() and int(r) >= min_rating):
            continue
        path = root / sk.SKILLS_DIR / f"{s['name']}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"""---
name: {s['name']}
description: {s['when'] or s['title']}
---
# {s['title']}

## Goal
{s['title']}.

## Acceptance criteria
""" + "\n".join(f"- {x}" for x in s["instructions"]) + f"""

## Resources & boundaries

## Known pitfalls
<!-- Inferred from {s['refinements']} refinements in {s['conversations']} conversations; rated {r}/5. -->
""", encoding="utf-8")
        written.append(path)
    return written


def compare(flags: list[Flags], labels: dict, since: datetime, skill_list: list[dict]) -> dict:
    """Before/after: refinements per 100 messages, overall and for applied skills."""
    refs = {r["id"]: r for r in refinements(flags, labels)}
    skill_toks = [tokens(" ".join(s["instructions"]) + " " + s["title"]) for s in skill_list]

    def covered(r):
        t = tokens(r["rule"] or r["text"])
        return any(len(t & st) >= 2 for st in skill_toks)

    out = {}
    for label, keep in (("before", lambda ts: ts < since), ("after", lambda ts: ts >= since)):
        fs = [f for f in flags if f.turn.ts and keep(f.turn.ts)]
        n = len(fs)
        rs = [refs[f.turn.id] for f in fs if f.turn.id in refs]
        convs = {(f.turn.source, f.turn.session) for f in fs}
        out[label] = {"messages": n, "conversations": len(convs),
                      "messages_per_conversation": round(n / len(convs), 1) if convs else None,
                      "refinements_per_100": round(100 * len(rs) / n, 1) if n else None,
                      "covered_refinements_per_100": round(100 * sum(covered(r) for r in rs) / n, 1) if n else None}
    return out
