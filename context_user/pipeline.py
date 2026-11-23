"""Field-study pipeline: two skill sets from your own history, shown blind.

  direct  one LLM step: history plus skill-writing guidance
  method  steps 1-7: steering moments, one-line rules, habits, recurrence tiers,
          counterexamples, skill text (each skill keeps evidence and reasoning)

Everything runs on your computer. The only outside call is `claude -p` with your own login.
Messages are masked (links, paths, emails) before they reach the model. Nothing here is uploaded.
"""
from __future__ import annotations

import json
import os
import random
import re
import subprocess
from pathlib import Path
from typing import Callable

from .sources import CLASSIFIER_MARKER, Turn

OUT = Path(".context/study_run")
MAX_CHARS = 600
SCOPE = ("Scope: only HOW the assistant should work with this person (explaining, answering, verifying, formats, "
         "workflow). Ignore specific personal topics such as job search, housing, travel, health, visas.")
GUIDANCE = """How to write a good skill:
- A skill is a short instruction file an AI assistant loads when a task matches.
- name: short, lowercase-hyphenated. description: one or two sentences saying what the skill does AND when to use it.
- Body: imperative rules, concise; say why when it is not obvious; one job per skill; no padding.
- Base every skill on evidence in the history; do not invent preferences; do not turn one-off decisions into rules."""

Runner = Callable[[str], str]


def mask(s: str) -> str:
    s = re.sub(r"https?://\S+", "[URL]", s)
    s = re.sub(r"(~|/Users|/home)?(/[\w.\-@ ()]+){2,}", "[path]", s)
    return re.sub(r"\S+@\S+\.\S+", "[email]", s)


def _valid(text: str) -> bool:
    for o, c in (("[", "]"), ("{", "}")):
        i, j = text.find(o), text.rfind(c)
        if i >= 0 and j > i:
            try:
                json.loads(text[i:j + 1])
                return True
            except ValueError:
                pass
    return False


USAGE = {"calls": 0, "input": 0, "output": 0, "cached": 0, "cost_usd": 0.0}
# No tools, no skills, no MCP, short system prompt: each call carries ~0.5k tokens of overhead instead of ~22k.
LEAN = ["--system-prompt", "You are a careful assistant. Follow the instructions in the user message exactly.",
        "--tools", "", "--strict-mcp-config", "--disable-slash-commands"]


def claude(prompt: str, model: str = "sonnet", expect_json: bool = True) -> str:
    """`claude -p` from the home folder; retries only when a JSON reply is not valid JSON."""
    last = ""
    for _ in range(3):
        out = subprocess.run(["claude", "-p", "--output-format", "json", "--model", model, "--no-session-persistence", *LEAN],
                             input=prompt, capture_output=True, text=True, timeout=900, cwd=Path.home(),
                             env={**os.environ, "CU_LEARNING": "1"})
        if out.returncode != 0:
            last = (out.stderr.strip() or out.stdout.strip())[:200]
            continue
        data = json.loads(out.stdout)
        u = data.get("usage", {})
        USAGE["calls"] += 1
        USAGE["input"] += u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
        USAGE["cached"] += u.get("cache_read_input_tokens", 0)
        USAGE["output"] += u.get("output_tokens", 0)
        USAGE["cost_usd"] += data.get("total_cost_usd", 0.0)
        res = data["result"]
        if not expect_json or _valid(res):
            return res
        last = "reply was not valid JSON: " + res[:120]
    raise RuntimeError(f"claude -p failed: {last}")


def jarr(text: str):
    return json.loads(text[text.find("["):text.rfind("]") + 1])


def jobj(text: str):
    return json.loads(text[text.find("{"):text.rfind("}") + 1])


SIGNAL = re.compile(r"不是|不对|不要|别|我说的是|重新|再|还是|没懂|不懂|什么意思|为什么|哪个|太长|简单|简短|直接|again|\bno,|not what|i said|shorter|simpler|simple|why|what do you mean|don'?t|instead|wrong|redo|rewrite|too (long|much)|more |less |actually|check|verify|correct|fix|improve|chinese|english|中文|英文", re.I)


def _priority(r: dict) -> bool:
    return bool(SIGNAL.search(r["text"])) or len(r["text"]) < 60


def _budget(train: list[dict], max_messages: int, seed: str = "") -> list[dict]:
    """Over budget: keep messages with a steering signal or that are short follow-ups first (up to 70%),
    fill the rest with a random sample of the others (a recall check), and keep time order."""
    if len(train) <= max_messages:
        return train
    pri = [r for r in train if _priority(r)]
    rest = [r for r in train if not _priority(r)]
    rnd = random.Random(seed or "budget")
    rnd.shuffle(pri)
    rnd.shuffle(rest)
    take = pri[:int(max_messages * 0.7)]
    take += rest[:max_messages - len(take)]
    take += pri[int(max_messages * 0.7):][:max_messages - len(take)]
    return sorted(take, key=lambda r: r["ts"])


def prepare(turns: list[Turn], holdout_days: int = 14, limit: int | None = None, since: str | None = None,
            until: str | None = None, max_messages: int | None = None) -> tuple[list[dict], list[dict], dict]:
    """Training rows and held-out rows (latest `holdout_days`), deduplicated, masked, with unique short ids."""
    seen, rows = set(), []
    for t in turns:
        text = (t.text or "").strip()
        if t.kind == "interrupt" or not text or CLASSIFIER_MARKER in text or t.ts is None:
            continue
        k = (t.ts.strftime("%Y-%m-%d %H:%M"), text)
        if k in seen:
            continue
        seen.add(k)
        rows.append({"turn": t, "ts": t.ts, "session": t.session, "src": t.source, "text": mask(text)[:MAX_CHARS], "prev": mask(t.prev[-600:])})
    cut = max(r["ts"] for r in rows) if rows else None
    if cut:
        from datetime import timedelta
        cut = cut - timedelta(days=holdout_days)
    train = [r for r in rows if not cut or r["ts"] < cut]
    held = [r for r in rows if cut and r["ts"] >= cut]
    if since:
        train = [r for r in train if f"{r['ts']:%Y-%m-%d}" >= since]
    if until:
        train = [r for r in train if f"{r['ts']:%Y-%m-%d}" <= until]
    in_range = len(train)
    if limit:
        train = train[-limit:]
    if max_messages:
        train = _budget(train, max_messages)
    for n, r in enumerate(train, 1):
        r["sid"] = f"m{n:03d}"
    for n, r in enumerate(held, 1):
        r["sid"] = f"h{n:03d}"
    return train, held, {"messages": len(rows), "in_range": in_range, "train": len(train), "held_out": len(held),
                         "signal_or_short": sum(1 for r in train if _priority(r))}


def direct(train: list[dict], run: Runner = claude) -> list[dict]:
    history = "\n".join(f"[{r['sid']}] {r['ts']:%Y-%m-%d} {r['text']}" for r in train)
    p = (f"{CLASSIFIER_MARKER} Below is a person's message history to AI assistants (their own messages only). "
         f"Write the skills that would best help an assistant work the way this person wants.\n\n{GUIDANCE}\n\n{SCOPE}\n\n"
         'Return ONLY a JSON array of up to 8 objects: {"name":"","kind":"skill|principle","description":"","body":"","evidence":["ids"]}\n\n'
         f"History:\n{history}")
    return jarr(run(p))


def steering(train: list[dict], run: Runner = claude, say=print) -> dict:
    """Steps 1-2: for every message, is it steering, and the one-line rule. Returns {sid: answer}."""
    P = (CLASSIFIER_MARKER + " For each item (the end of the AI's previous reply 'ai', the person's message 'me') decide if the person steers "
         "the AI: corrects, pushes back, redirects, asks for a different form, or states a standing preference. Also count repeating a request "
         "in a new form, or asking for simpler, shorter, more direct, more checked. Only pasted material and plain new tasks are not steering. "
         'Return ONLY a JSON array: {"id":"","steer":true|false,"rule":"one short imperative rule, empty if not steering","scope":"global|project|one-off"}\n'
         + SCOPE + "\nItems:\n")
    out = {}
    for i in range(0, len(train), 25):
        ch = train[i:i + 25]
        items = json.dumps([{"id": r["sid"], "ai": r["prev"][-600:], "me": r["text"][:500]} for r in ch], ensure_ascii=False)
        for o in jarr(run(P + items)):
            out[str(o.get("id"))] = o
        say(f"steps 1-2: {min(i + 25, len(train))}/{len(train)}")
    return out


def kit_moments(train: list[dict], root: Path, classify=None, say=print) -> list[dict]:
    """Steps 1-2 with the kit's own classifier: haiku, sees the AI's last 600 characters, labels cached per message."""
    from . import llm
    labels = (classify or llm.classify)(root, [r["turn"] for r in train], model="haiku", say=say)
    out = []
    for r in train:
        lab = labels.get(llm.key(r["turn"]))
        if lab and (lab["correction"] or lab["refine"] or lab["repeat"]) and lab["lesson"]:
            out.append({"id": r["sid"], "date": f"{r['ts']:%Y-%m-%d}", "session": r["session"], "rule": lab["lesson"], "scope": lab["scope"]})
    return out


def method(train: list[dict], run: Runner = claude, say=print, moments: list[dict] | None = None) -> dict:
    by = {r["sid"]: r for r in train}
    if moments is None:
        moments = []
        for k, o in steering(train, run, say).items():
            r = by.get(k)
            if r and o.get("steer") and o.get("rule"):
                moments.append({"id": r["sid"], "date": f"{r['ts']:%Y-%m-%d}", "session": r["session"], "rule": o["rule"], "scope": o.get("scope")})
    # habits
    PH = (CLASSIFIER_MARKER + " Find recurring HABITS in how this person asks and works (request shapes, what they check, formats they want). "
          'A habit must show in 2+ messages. Return ONLY a JSON array of {"habit":"","ids":[""],"implication":""}.\n' + SCOPE + "\nMessages:\n")
    habits = []
    for i in range(0, len(train), 120):
        habits += jarr(run(PH + "\n".join(f"[{r['sid']}] s={r['session'][:6]} {r['text'][:220]}" for r in train[i:i + 120])))
    # steps 3-5
    lines = "\n".join(f"[{m['id']}] {m['date']} s={m['session'][:6]} ({m['scope']}) {m['rule']}" for m in moments)
    P35 = (CLASSIFIER_MARKER + " Rules extracted from one person's steering moments. Group rules that say the same thing; tier each group "
           "(strong = 3+ sessions, medium = 2, weak = 1 but a clear global preference); if a later rule reverses an earlier one keep the later; "
           "state the principle behind each group. Keep all tiers; reject only one-off or personal-topic items. Habit observations may support or form candidates.\n"
           'Return ONLY JSON: {"candidates":[{"name":"","kind":"principle|skill","statement":"","tier":"strong|medium|weak","evidence":["ids"],"sessions":0,"reasoning":"<=40 words"}],'
           '"rejected":[{"idea":"","reason":""}]}\n' + SCOPE + "\nRules:\n" + lines + "\n\nHabits:\n"
           + "\n".join(f"- {h.get('habit')} => {h.get('implication')} ({','.join(h.get('ids', []))})" for h in habits))
    cand = jobj(run(P35))
    # step 6: counterexamples
    steer = {m["id"] for m in moments}
    rest = [x for x in train if x["sid"] not in steer][:300]
    others = "\n".join(f"[{r['sid']}] {r['text'][:200]}" for r in rest)
    P6 = (CLASSIFIER_MARKER + " For each candidate, look in these other messages (which did NOT steer) for counterexamples: cases where the person wanted the opposite, "
          'or where it would not apply. Return ONLY a JSON array: [{"name":"","counterexamples":["ids"],"verdict":"keep|narrow|drop","narrowed_statement":"","note":""}]\nCandidates:\n'
          + json.dumps([{k: c[k] for k in ("name", "statement")} for c in cand["candidates"]], ensure_ascii=False) + "\nMessages:\n" + others)
    v6 = {o["name"]: o for o in jarr(run(P6))}
    final = []
    for c in cand["candidates"]:
        v = v6.get(c["name"], {})
        if v.get("verdict") == "drop":
            cand["rejected"].append({"idea": c["name"], "reason": "dropped at counterexample step: " + str(v.get("note"))})
            continue
        if v.get("verdict") == "narrow" and v.get("narrowed_statement"):
            c["statement"] = v["narrowed_statement"]
        c["counterexamples"], c["counter_note"] = v.get("counterexamples", []), v.get("note", "")
        final.append(c)
    # step 7: skill text
    P7 = (CLASSIFIER_MARKER + ' Write each candidate as a skill file. Return ONLY a JSON array of {"name":"","description":"what and WHEN, 1-2 sentences","body":"short imperative rules, no message ids"} in the same order.\n'
          + json.dumps([{k: c[k] for k in ("name", "kind", "statement")} for c in final], ensure_ascii=False))
    for c, w in zip(final, jarr(run(P7))):
        c["description"], c["body"] = w.get("description"), w.get("body")
    return {"skills": final, "rejected": cand["rejected"], "moments": len(moments), "habits": len(habits)}


def blind(direct_skills: list[dict], method_skills: list[dict], seed: str = "") -> tuple[dict, dict]:
    """Shuffle the two sets into X and Y; the key stays in a separate file."""
    order = ["direct", "method"]
    random.Random(seed or os.urandom(8).hex()).shuffle(order)
    sets = {"direct": direct_skills, "method": method_skills}
    return ({"X": sets[order[0]], "Y": sets[order[1]]}, {"X": order[0], "Y": order[1]})


def run(turns: list[Turn], root: Path, holdout_days: int = 14, limit: int | None = None, runner: Runner = claude, say=print,
        since: str | None = None, until: str | None = None, max_messages: int | None = 300, classify=None, use_kit: bool = True) -> dict:
    train, held, counts = prepare(turns, holdout_days, limit, since, until, max_messages)
    say(f"{counts['train']} of {counts['in_range']} messages in range used ({counts['signal_or_short']} with a steering signal or short); {counts['held_out']} held out (last {holdout_days} days)")
    out = root / OUT
    out.mkdir(parents=True, exist_ok=True)
    pid = out / "participant.txt"
    if not pid.exists():  # a random pseudonym, not a name; the participant may replace it with their own code
        pid.write_text("p" + os.urandom(3).hex(), encoding="utf-8")
    d = direct(train, runner)
    moments = kit_moments(train, root, classify, say) if use_kit else None
    m = method(train, runner, say, moments)
    sets, key = blind(d, m["skills"])
    (out / "skills_blind.json").write_text(json.dumps(sets, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "key.json").write_text(json.dumps(key), encoding="utf-8")
    (out / "method_detail.json").write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "heldout.json").write_text(json.dumps([{"sid": r["sid"], "ts": f"{r['ts']:%Y-%m-%d}", "text": r["text"], "prev": r["prev"]} for r in held], ensure_ascii=False, indent=1), encoding="utf-8")
    counts["tokens"] = {k: (round(v, 3) if isinstance(v, float) else v) for k, v in USAGE.items()}
    counts.update({"direct_skills": len(d), "method_skills": len(m["skills"]), "moments": m["moments"]})
    return counts


FILE_REFS = re.compile(r"\.(md|py|json|csv|pdf|js|ts|ipynb)\b|\[path\]|\[URL\]|\brun\b|\bscript\b|\brepo\b|\bfile\b|@\S", re.I)
SHELL = ("{marker} {skills}Answer the task below directly in text, as you would for this person. "
         "Do not use tools or ask to read files.\n\nTask:\n{task}")


def _skills_text(items: list[dict]) -> str:
    out = []
    for s in items:
        body = re.sub(r"\s*\(m\d{3}(,\s*m\d{3})*\)", "", s.get("body") or s.get("statement") or "")
        out.append(f"### {s['name']}\n{s.get('description', '')}\n{body}")
    return "Skills you have loaded for this person (follow them when they apply):\n\n" + "\n\n".join(out) + "\n\n"


def pick_tasks(held: list[dict], n: int, seed: str = "") -> list[dict]:
    """Held-out messages that can be answered from the text alone (no files, links or commands)."""
    ok = [h for h in held if 80 <= len(h["text"]) <= 1500 and not FILE_REFS.search(h["text"])]
    random.Random(seed or "tasks").shuffle(ok)
    return sorted(ok[:n], key=lambda h: h["ts"])


def answers(root: Path, n: int = 5, runner: Runner | None = None, say=print) -> int:
    """Answer n of your held-out tasks under three conditions; shuffle to A/B/C; key stays in a separate file."""
    runner = runner or (lambda p: claude(p, expect_json=False))
    out = root / OUT
    held = json.loads((out / "heldout.json").read_text(encoding="utf-8"))
    key = json.loads((out / "key.json").read_text())
    sets = json.loads((out / "skills_blind.json").read_text(encoding="utf-8"))
    cond = {"none": "", key["X"]: _skills_text(sets["X"]), key["Y"]: _skills_text(sets["Y"])}
    tasks = pick_tasks(held, n)
    sheet, akey = [], {}
    rnd = random.Random(os.urandom(8).hex())
    for i, t in enumerate(tasks, 1):
        res = {c: runner(SHELL.format(marker=CLASSIFIER_MARKER, skills=s, task=t["text"])) for c, s in cond.items()}
        order = list(res)
        rnd.shuffle(order)
        akey[str(i)] = dict(zip("ABC", order))
        sheet.append({"id": i, "prompt": t["text"], "answers": {L: res[c] for L, c in zip("ABC", order)}})
        say(f"answers: task {i}/{len(tasks)}")
    (out / "answers_blind.json").write_text(json.dumps(sheet, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "answers_key.json").write_text(json.dumps(akey), encoding="utf-8")
    return len(sheet)
