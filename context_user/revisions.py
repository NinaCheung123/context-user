"""Learn from how the developer revises the agent's drafts.

Two signals that never show up as a "correction" message:

  paste  the developer sends back a revised version of something the agent
         wrote (a proposal, an email): the message is similar to an earlier
         agent draft, but changed
  file   the agent wrote a file, and its current content differs from what
         the agent left: the developer edited it by hand

For each revision we keep the sentences removed and added. `classify`
then asks Claude what the edit teaches about the developer's preferences
(or nothing, if it is a one-off content change).
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .detect import jaccard, tokens
from .sources import CLASSIFIER_MARKER, CLAUDE_DIR, Turn, _parse_ts

MIN_DRAFT = 300          # characters; shorter agent text is not a "draft"
SIMILAR = (0.35, 0.97)   # Jaccard range: clearly based on the draft, but changed
MIN_CHANGE = 1           # changed sentences needed to count as a revision
CACHE = Path(".context/llm_revisions.jsonl")


@dataclass
class Draft:
    session: str
    ts: datetime | None
    text: str
    origin: str                      # "reply" or the file path the agent wrote
    toks: frozenset = field(default_factory=frozenset, repr=False)


@dataclass
class Revision:
    kind: str                        # "paste" or "file"
    session: str
    ts: datetime | None
    origin: str                      # "reply", or the file path
    similarity: float
    removed: list[str]
    added: list[str]

    @property
    def key(self) -> str:
        body = "\n".join(self.removed) + "\n--\n" + "\n".join(self.added)
        return hashlib.sha1(f"{self.kind}:{self.origin}:{body}".encode()).hexdigest()[:16]


CJK = r"\u3000-\u303f\u4e00-\u9fff\uff00-\uffef"


def sentences(text: str) -> list[str]:
    """Normalized sentences/lines, so formatting differences don't count as edits.

    Copying text out of a chat drops Markdown and changes spacing and quote
    marks; none of that is an edit.
    """
    text = re.sub(r"[*_`#>|]", " ", text)
    text = text.translate(str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"'}))
    text = re.sub(rf"(?<=[{CJK}])[ \t]+|[ \t]+(?=[{CJK}])", "", text)  # spaces next to CJK
    parts = re.split(r"(?<=[.!?。！？；;])\s+|\n+", text)
    return [" ".join(p.split()) for p in parts if len(p.split()) >= 3 or len(p.strip()) >= 8]


def diff(old: str, new: str) -> tuple[list[str], list[str]]:
    a, b = sentences(old), sentences(new)
    removed, added = [], []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op in ("replace", "delete"):
            removed += a[i1:i2]
        if op in ("replace", "insert"):
            added += b[j1:j2]
    return removed, added


def _apply_edit(content: str, inp: dict) -> str:
    old, new = inp.get("old_string", ""), inp.get("new_string", "")
    if not old:
        return content
    return content.replace(old, new) if inp.get("replace_all") else content.replace(old, new, 1)


def read_drafts(root: Path = CLAUDE_DIR) -> tuple[list[Draft], dict[str, tuple]]:
    """Agent replies long enough to be drafts, and the last content the agent
    left in each file it wrote: {path: (session, ts, content)}.

    A file the agent later touched through a shell command (sed, a script,
    pandoc, ...) is dropped: we can't know what the agent left in it, and
    its later changes would be misread as the developer's edits.
    """
    drafts: list[Draft] = []
    files: dict[str, tuple] = {}
    shell: list[tuple] = []  # (ts, command)
    for path in sorted(root.glob("*/*.jsonl")):
        with path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") != "assistant" or entry.get("isSidechain"):
                    continue
                session, ts = entry.get("sessionId", path.stem), _parse_ts(entry.get("timestamp"))
                for b in entry.get("message", {}).get("content") or []:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text" and len(b.get("text", "")) >= MIN_DRAFT:
                        drafts.append(Draft(session, ts, b["text"], "reply"))
                    elif b.get("type") == "tool_use":
                        inp = b.get("input") or {}
                        fp = inp.get("file_path")
                        if b.get("name") == "Write" and fp:
                            files[fp] = (session, ts, inp.get("content", ""))
                            if len(inp.get("content", "")) >= MIN_DRAFT:
                                drafts.append(Draft(session, ts, inp["content"], fp))
                        elif b.get("name") == "Edit" and fp in files:
                            s, _, content = files[fp]
                            files[fp] = (s, ts, _apply_edit(content, inp))
                        elif b.get("name") == "Bash":
                            shell.append((ts, str(inp.get("command", ""))))
    for fp, (_, wrote, _) in list(files.items()):
        name = Path(fp).name
        if any(name in cmd and (not wrote or not ts or ts > wrote) for ts, cmd in shell):
            del files[fp]
    for d in drafts:
        d.toks = tokens(d.text)
    return drafts, files


def chat_drafts(replies: list[tuple]) -> list[Draft]:
    """Drafts from a claude.ai export's assistant replies."""
    return [Draft(s, ts, text, "reply", tokens(text)) for s, ts, text in replies if len(text) >= MIN_DRAFT]


def pasted_revisions(turns: list[Turn], drafts: list[Draft]) -> list[Revision]:
    """User messages that are edited versions of an earlier agent draft."""
    out = []
    for t in turns:
        # Quoted lines ("> ...") are the agent's own text being replied to, not a revision.
        own = "\n".join(l for l in t.text.splitlines() if not l.lstrip().startswith(">"))
        if len(own) < MIN_DRAFT or t.kind == "interrupt":
            continue
        tt = tokens(own)
        best, score = None, 0.0
        for d in drafts:
            if d.ts and t.ts and d.ts >= t.ts:
                continue
            s = jaccard(tt, d.toks)
            if s > score:
                best, score = d, s
        if best and SIMILAR[0] <= score <= SIMILAR[1]:
            removed, added = diff(best.text, own)
            if len(removed) + len(added) >= MIN_CHANGE:
                out.append(Revision("paste", t.session, t.ts, best.origin, round(score, 2), removed, added))
    return out


def file_revisions(files: dict[str, tuple]) -> list[Revision]:
    """Files whose current content differs from what the agent last left."""
    out = []
    for fp, (session, ts, agent_content) in files.items():
        p = Path(fp)
        if not p.is_file():
            continue
        try:
            now = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if now == agent_content:
            continue
        removed, added = diff(agent_content, now)
        if len(removed) + len(added) >= MIN_CHANGE:
            out.append(Revision("file", session, ts, fp, round(jaccard(tokens(agent_content), tokens(now)), 2),
                                removed, added))
    return out


PROMPT = """{marker} A developer revised drafts written by their AI agent. For each item you get
sentences the developer REMOVED from the agent's draft and sentences they ADDED.
Return ONLY a JSON array, one object per item, in order:
{{"id": "<id>", "lessons": [{{"lesson": "<rule>", "scope": "global|project|one-off"}}, ...]}}
- lessons: up to 3 short imperative rules about the developer's preferences that a future agent
  should follow, most important first. Look for both style (wording, tone, length, hedging) and
  substance (framing, priorities, what the document is mainly about). Write them in the developer's
  language. Use [] if the edit only changes facts or content specific to this one document.
- scope: "global" (applies to all work), "project" (this project), or "one-off".

Items:
{items}"""


def load_cache(root: Path) -> dict[str, dict]:
    p = root / CACHE
    if not p.exists():
        return {}
    return {r["key"]: r for r in (json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip())}


def classify(root: Path, revs: list[Revision], model: str = "haiku", runner=None, say=print) -> dict[str, dict]:
    from .llm import _claude, _parse
    runner = runner or _claude
    cache = load_cache(root)
    todo = [r for r in revs if r.key not in cache]
    path = root / CACHE
    path.parent.mkdir(parents=True, exist_ok=True)
    for i in range(0, len(todo), 8):
        chunk = todo[i:i + 8]
        items = json.dumps([{"id": str(n), "removed": r.removed[:25], "added": r.added[:25]}
                            for n, r in enumerate(chunk)], ensure_ascii=False, indent=1)
        labels = {str(l.get("id")): l for l in _parse(runner(PROMPT.format(marker=CLASSIFIER_MARKER, items=items), model))}
        with path.open("a", encoding="utf-8") as f:
            for n, r in enumerate(chunk):
                l = labels.get(str(n), {})
                found = [x for x in (l.get("lessons") or []) if isinstance(x, dict) and x.get("lesson")]
                if not found and l.get("lesson"):  # tolerate the single-lesson shape
                    found = [{"lesson": l["lesson"], "scope": l.get("scope", "one-off")}]
                row = {"key": r.key, "kind": r.kind, "origin": r.origin, "model": model,
                       "lessons": [{"lesson": str(x["lesson"]).strip(), "scope": x.get("scope", "one-off")}
                                   for x in found]}
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                cache[r.key] = row
        say(f"classified {min(i + 8, len(todo))}/{len(todo)} revisions")
    return cache
