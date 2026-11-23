"""Optional LLM classifier for friction, via the developer's own `claude` CLI.

Keyword rules miss most pushback in design conversations ("I still think…",
"usually I…"), because whether a message is a correction depends on what the
agent just said. This sends each message with the end of the agent's
previous reply to Claude in headless mode and asks for:

  correction  the developer pushes back on / corrects / redirects the agent
  repeat      it states a standing preference likely to be needed again
  lesson      a one-line imperative rule for future agents, or ""

Results are cached in .context/llm_labels.jsonl (keyed by message hash), so
each message is classified once. Calls use --no-session-persistence and a
marker, so they never show up as study data.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

from .sources import CLASSIFIER_MARKER, Turn

CACHE = Path(".context/llm_labels.jsonl")
BATCH = 15
MAX_TEXT = 1500

PROMPT = """{marker} You label messages that a developer sent to an AI coding agent.
For each item you get the end of what the agent said just before ("agent", may be empty)
and the developer's message ("dev"). Return ONLY a JSON array with one object per item, in order:
{{"id": "<id>", "refine": true|false, "correction": true|false, "repeat": true|false,
  "lesson": "<rule or empty>", "scope": "global|project|one-off"}}

- refine: the developer asks to change or improve the agent's previous output (e.g., shorter,
  different tone or format, add or remove something, fix a detail), rather than starting a new task.
- correction: the developer pushes back on, corrects, or redirects what the agent did or proposed,
  including disagreeing with a plan or design. New requests, neutral questions, and forwarded or
  pasted material are NOT corrections.
- repeat: the message states a standing preference or instruction the developer would likely
  have to give again in a future session.
- lesson: if refine, correction or repeat is true, one short imperative rule a future agent should follow,
  in the developer's language; otherwise "".
- scope: "global" (applies to all the developer's work), "project" (this project only), or
  "one-off" (a decision about this one task that a future session would not need).

Items:
{items}"""


def key(turn: Turn) -> str:
    return hashlib.sha1(f"{turn.id}\n{turn.text}".encode()).hexdigest()[:16]


def load_cache(root: Path) -> dict[str, dict]:
    path = root / CACHE
    if not path.exists():
        return {}
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    return {r["key"]: r for r in rows}


def _claude(prompt: str, model: str) -> str:
    # Run from the home folder (no project hooks load) and with CU_LEARNING set,
    # so a SessionEnd hook that runs `cu learn` can never trigger itself again.
    out = subprocess.run(
        ["claude", "-p", "--output-format", "json", "--model", model, "--no-session-persistence",
         # no tools, skills or MCP and a short system prompt: ~0.5k tokens per call instead of ~22k
         "--system-prompt", "You are a careful assistant. Follow the instructions in the user message exactly.",
         "--tools", "", "--strict-mcp-config", "--disable-slash-commands"],
        input=prompt, capture_output=True, text=True, timeout=600, cwd=Path.home(),
        env={**os.environ, "CU_LEARNING": "1"})
    if out.returncode != 0:
        raise RuntimeError(f"claude -p failed: {out.stderr.strip()[:300]}")
    return json.loads(out.stdout)["result"]


def _parse(result: str) -> list[dict]:
    start, end = result.find("["), result.rfind("]")
    if start < 0 or end < start:
        raise ValueError(f"no JSON array in classifier output: {result[:200]!r}")
    return json.loads(result[start:end + 1])


def classify(root: Path, turns: list[Turn], model: str = "haiku", runner=_claude,
             batch: int = BATCH, say=print) -> dict[str, dict]:
    """Label every not-yet-labelled turn; returns the full cache."""
    cache = load_cache(root)
    todo = [t for t in turns if t.kind != "interrupt" and key(t) not in cache]
    path = root / CACHE
    path.parent.mkdir(parents=True, exist_ok=True)
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        items = json.dumps([{"id": str(n), "agent": t.prev[-600:], "dev": t.text[:MAX_TEXT]}
                            for n, t in enumerate(chunk)], ensure_ascii=False, indent=1)
        labels = {str(l.get("id")): l for l in _parse(runner(PROMPT.format(marker=CLASSIFIER_MARKER, items=items), model))}
        rows = []
        for n, t in enumerate(chunk):
            l = labels.get(str(n), {})
            rows.append({"key": key(t), "turn": t.id, "session": t.session, "model": model,
                         "refine": bool(l.get("refine")),
                         "correction": bool(l.get("correction")), "repeat": bool(l.get("repeat")),
                         "lesson": str(l.get("lesson") or "").strip(),
                         "scope": l.get("scope") if l.get("scope") in ("global", "project", "one-off") else "unknown"})
        with path.open("a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
                cache[r["key"]] = r
        say(f"classified {min(i + batch, len(todo))}/{len(todo)}")
    return cache
