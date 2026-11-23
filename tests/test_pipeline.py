import json
from datetime import datetime, timedelta, timezone

from context_user import pipeline
from context_user.sources import Turn


def _turns(n=60):
    base = datetime(2026, 10, 1, tzinfo=timezone.utc)
    return [Turn("claude", f"s{i % 4}", "p", base + timedelta(hours=i * 12), i, f"msg {i} see https://x.io/a and /Users/me/f.py", prev="ai said") for i in range(n)]


def fake(prompt: str) -> str:
    if "decide if the person steers" in prompt:
        ids = [x["id"] for x in json.loads(prompt.split("Items:\n")[1])]
        return json.dumps([{"id": i, "steer": True, "rule": "be brief", "scope": "global"} for i in ids])
    if "recurring HABITS" in prompt:
        return json.dumps([{"habit": "asks short", "ids": ["m001"], "implication": "answer short"}])
    if "Group rules" in prompt:
        return json.dumps({"candidates": [{"name": "be-brief", "kind": "principle", "statement": "Be brief.", "tier": "strong", "evidence": ["m001"], "sessions": 3, "reasoning": "x"}], "rejected": []})
    if "counterexamples" in prompt:
        return json.dumps([{"name": "be-brief", "counterexamples": [], "verdict": "keep", "narrowed_statement": "", "note": ""}])
    if "Write each candidate" in prompt:
        return json.dumps([{"name": "be-brief", "description": "when", "body": "Be brief."}])
    return json.dumps([{"name": "d1", "kind": "principle", "description": "x", "body": "y", "evidence": ["m001"]}])


def test_prepare_masks_dedups_and_holds_out():
    train, held, counts = pipeline.prepare(_turns() + _turns(2), holdout_days=5)
    assert counts["held_out"] > 0 and counts["train"] + counts["held_out"] == counts["messages"] == 60
    assert all("https://" not in r["text"] and "/Users/" not in r["text"] for r in train)
    assert len({r["sid"] for r in train}) == len(train)


def test_run_writes_blind_sets_and_key(tmp_path):
    def fake_classify(root, turns, model="haiku", say=print):
        from context_user import llm
        return {llm.key(x): {"correction": True, "refine": False, "repeat": False, "lesson": "be brief", "scope": "global"} for x in turns}
    counts = pipeline.run(_turns(), tmp_path, holdout_days=5, runner=fake, say=lambda *_: None, classify=fake_classify)
    out = tmp_path / pipeline.OUT
    sets, key = json.loads((out / "skills_blind.json").read_text()), json.loads((out / "key.json").read_text())
    assert set(sets) == {"X", "Y"} and sorted(key.values()) == ["direct", "method"]
    assert counts["method_skills"] == 1 and counts["direct_skills"] == 1


def test_budget_keeps_signals_first_and_time_order():
    from datetime import datetime, timedelta, timezone
    base = datetime(2026, 10, 1, tzinfo=timezone.utc)
    turns = [Turn("claude", f"s{i % 5}", "p", base + timedelta(hours=i * 12), i,
                  ("please make it shorter, not what I said " if i % 4 == 0 else "a plain long request about a topic number ") + str(i) + " " * 0 + "x" * 80)
             for i in range(200)]
    train, _, counts = pipeline.prepare(turns, holdout_days=0, max_messages=60)
    assert len(train) == 60 and counts["in_range"] >= 199
    assert [r["ts"] for r in train] == sorted(r["ts"] for r in train)
    assert sum(1 for r in train if "shorter" in r["text"]) >= 40
    t2, _, c2 = pipeline.prepare(turns, holdout_days=0, since="2026-10-20", until="2026-10-25")
    assert all("2026-10-20" <= f"{r['ts']:%Y-%m-%d}" <= "2026-10-25" for r in t2)
