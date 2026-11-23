import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from context_user import detect, effect, history, lessons, metrics, skills, sources, sync
from context_user.sources import ToolUse, Turn

T0 = datetime(2026, 10, 20, 9, tzinfo=timezone.utc)


def turn(text, session="s1", index=1, minutes=0, source="claude"):
    return Turn(source, session, "/proj", T0 + timedelta(minutes=minutes), index, text)


# ------------------------------------------------------------------ sources

def test_read_claude_keeps_only_typed_prompts(tmp_path):
    proj = tmp_path / "-proj"
    proj.mkdir()
    lines = [
        {"type": "user", "sessionId": "a", "cwd": "/proj", "timestamp": "2026-10-20T09:00:00Z",
         "message": {"content": "Add a login page"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}},
        {"type": "user", "sessionId": "a", "message": {"content": [{"type": "tool_result", "content": "x"}]}},
        {"type": "user", "sessionId": "a", "message": {"content": "<command-name>/clear</command-name>"}},
        {"type": "user", "sessionId": "a", "isSidechain": True, "message": {"content": "subagent prompt"}},
        {"type": "user", "sessionId": "a", "timestamp": "2026-10-20T09:05:00Z",
         "message": {"content": [{"type": "text", "text": "No, use pnpm"}]}},
    ]
    (proj / "a.jsonl").write_text("\n".join(json.dumps(l) for l in lines) + "\nnot json\n")
    got = list(sources.read_claude(tmp_path))
    assert [(t.index, t.text) for t in got] == [(0, "Add a login page"), (1, "No, use pnpm")]
    assert got[0].ts.year == 2026


def test_read_cursor_both_layouts(tmp_path):
    db_dir = tmp_path / "globalStorage"
    db_dir.mkdir()
    con = sqlite3.connect(db_dir / "state.vscdb")
    con.execute("CREATE TABLE cursorDiskKV (key TEXT, value TEXT)")
    con.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", ("composerData:c1", json.dumps(
        {"createdAt": 1792400000000, "conversation": [
            {"type": 1, "text": "fix the bug"}, {"type": 2, "text": "done"}, {"type": 1, "text": "wrong file"}]})))
    con.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", ("bubbleId:c2:b1", json.dumps({"type": 1, "text": "hello"})))
    con.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", ("bubbleId:c2:b2", "not json"))
    con.commit()
    con.close()
    got = list(sources.read_cursor(tmp_path))
    assert [(t.session, t.index, t.text) for t in got] == [
        ("c1", 0, "fix the bug"), ("c1", 1, "wrong file"), ("c2", 0, "hello")]


def test_read_cursor_missing_is_empty(tmp_path):
    assert list(sources.read_cursor(tmp_path)) == []


# ------------------------------------------------------------------- detect

@pytest.mark.parametrize("text", [
    "No, that's the wrong file",
    "I already told you to use pnpm",
    "it's still failing with the same error",
    "please revert that change",
    "don't add new dependencies",
    "不对，我说过用 pnpm",
])
def test_corrections_flagged(text):
    assert detect.find_correction(turn(text)) is not None


@pytest.mark.parametrize("text", [
    "Add a dark mode toggle to the settings page",
    "Now write tests for the parser",
    "looks good, commit it",
])
def test_normal_requests_not_flagged(text):
    assert detect.find_correction(turn(text)) is None


def test_opening_prompt_never_a_correction():
    assert detect.find_correction(turn("No tests exist yet, add some", index=0)) is None


def test_repeat_across_sessions_only():
    text = "Always use pnpm instead of npm and run the vitest suite before committing"
    flags = detect.analyze([
        turn(text, session="s1", index=0),
        turn(text, session="s1", index=1, minutes=1),           # same session: not a repeat
        turn(text + " please", session="s2", index=0, minutes=60),
    ])
    assert [f.repeat_of for f in flags] == [None, None, "claude:s1:1"]  # most recent match


def test_short_turns_never_repeats():
    flags = detect.analyze([turn("continue", "s1", 0), turn("continue", "s2", 0, 5)])
    assert flags[1].repeat_of is None


# --------------------------------------------------------------------- sync

def test_sync_renders_native_files(tmp_path):
    sync.init(tmp_path)
    (tmp_path / ".context/PROJECT.md").write_text("# P\n- Use pnpm\n")
    skills.create(tmp_path, "Release Process", "Use when cutting a release.")
    written = sync.sync(tmp_path, ["claude", "cursor", "agents"])
    claude = (tmp_path / "CLAUDE.md").read_text()
    cursor = (tmp_path / ".cursor/rules/context-user.mdc").read_text()
    skill_md = (tmp_path / ".claude/skills/release-process/SKILL.md").read_text()
    cursor_skill = (tmp_path / ".cursor/rules/skill-release-process.mdc").read_text()
    agents = (tmp_path / "AGENTS.md").read_text()
    assert len(written) == 5
    assert "Use pnpm" in claude and sync.MARKER in claude
    assert "`.context/skills/release-process.md`" in claude and "release-process" in cursor  # router
    assert cursor.startswith("---\n") and "alwaysApply: true" in cursor
    assert skill_md.startswith("---\nname: release-process\ndescription: Use when cutting a release.\n---")
    assert "alwaysApply: false" in cursor_skill and "## Known pitfalls" in cursor_skill
    assert "`.context/skills/release-process.md`" in agents  # generated index


def test_sync_prunes_deleted_skills_and_clean_removes_all(tmp_path):
    sync.init(tmp_path)
    path = skills.create(tmp_path, "deploy", "Use when deploying.")
    sync.sync(tmp_path)
    path.unlink()
    sync.sync(tmp_path)
    assert not (tmp_path / ".claude/skills/deploy").exists()
    assert not (tmp_path / ".cursor/rules/skill-deploy.mdc").exists()
    removed = sync.clean(tmp_path)
    assert {p.name for p in removed} == {"CLAUDE.md", "context-user.mdc"}
    assert (tmp_path / ".context/PROJECT.md").exists()  # source untouched


def test_sync_refuses_handwritten_file(tmp_path):
    sync.init(tmp_path)
    (tmp_path / "CLAUDE.md").write_text("my own notes")
    with pytest.raises(FileExistsError):
        sync.sync(tmp_path, ["claude"])
    assert (tmp_path / "CLAUDE.md").read_text() == "my own notes"
    sync.sync(tmp_path, ["claude"], force=True)
    assert sync.MARKER in (tmp_path / "CLAUDE.md").read_text()


# ------------------------------------------------------------------- skills

def test_add_pitfall_goes_inside_section(tmp_path):
    path = skills.create(tmp_path, "release", "Use when releasing.")
    with path.open("a") as f:
        f.write("\n## Notes\nkeep me last\n")
    skills.add_pitfall(path, "- Bump the changelog first")
    text = path.read_text()
    assert text.index("Bump the changelog") < text.index("## Notes")
    assert text.index("## Known pitfalls") < text.index("Bump the changelog")


def test_suggest_matches_relevant_skill(tmp_path):
    skills.create(tmp_path, "testing", "Use when writing or running the vitest test suite.")
    skills.create(tmp_path, "release", "Use when publishing a new npm version.")
    found = skills.suggest("Run vitest suite with --run so the tests do not watch", skills.load(tmp_path))
    assert found.name == "testing"
    assert skills.suggest("hello there", skills.load(tmp_path)) is None


def test_recurring_tasks_need_several_sessions():
    opener = "Write unit tests for the parser module and run them"
    flags = detect.analyze([turn(opener + extra, f"s{i}", 0, i * 60)
                            for i, extra in enumerate(["", " please", " again", ""])])
    assert skills.recurring_tasks(flags, [], min_sessions=3)[0]["sessions"] == 4
    assert skills.recurring_tasks(flags, [], min_sessions=5) == []


# ------------------------------------------------------------------ lessons

def friction():
    return detect.analyze([
        turn("Build the API", "s1", 0),
        turn("No, use pnpm not npm for installing packages", "s1", 1),
        turn("Build the UI", "s2", 0, 60),
        turn("No, use pnpm not npm for installing deps", "s2", 1, 61),
        turn("Looks good", "s2", 2, 62),
    ])


def test_candidates_group_similar_friction():
    cands = lessons.candidates(friction())
    assert cands[0]["count"] == 2 and cands[0]["kind"] == "correction"


def test_add_global_and_into_skill(tmp_path):
    sync.init(tmp_path)
    lessons.add(tmp_path, "Use pnpm, never npm")
    assert "- Use pnpm, never npm" in (tmp_path / ".context/LESSONS.md").read_text()
    skills.create(tmp_path, "release", "Use when releasing.")
    lessons.add(tmp_path, "Bump the changelog first", skill="release")
    assert "- Bump the changelog first" in (tmp_path / ".context/skills/release.md").read_text()
    with pytest.raises(FileNotFoundError):
        lessons.add(tmp_path, "x", skill="nope")


def test_place_guesses_skill_or_global(tmp_path):
    sync.init(tmp_path)
    skills.create(tmp_path, "testing", "Use when writing or running the vitest test suite.")
    assert lessons.place(tmp_path, "run vitest suite with --run") == "testing"
    assert lessons.place(tmp_path, "be concise") == "global"


# ------------------------------------------------------------------ history

def tool(name, minutes, session="s1", **inp):
    return ToolUse(session, T0 + timedelta(minutes=minutes), name, inp)


def test_snapshot_records_events_and_channels(tmp_path):
    sync.init(tmp_path)
    first = history.snapshot(tmp_path, at=T0)
    assert first and all(e["channel"] == "initial" for e in first)
    assert history.snapshot(tmp_path, at=T0 + timedelta(minutes=1)) == []  # no change, no event

    lessons.add(tmp_path, "Use pnpm, never npm")  # agent edits LESSONS.md in a session
    agent = [tool("Edit", 5, file_path=str(tmp_path / ".context/LESSONS.md"))]
    e = history.snapshot(tmp_path, agent, at=T0 + timedelta(minutes=10))
    assert e[0]["file"] == "LESSONS.md" and e[0]["channel"] == "conversation"
    assert e[0]["added"][0]["text"] == "- Use pnpm, never npm"

    path = tmp_path / ".context/PROJECT.md"  # developer edits a file by hand
    path.write_text(path.read_text().replace("- Never do:", "- Never do: force-push main"))
    e = history.snapshot(tmp_path, agent, at=T0 + timedelta(minutes=20))
    assert e[0]["channel"] == "manual"
    assert e[0]["removed"] == ["- Never do:"] and e[0]["added"][0]["text"] == "- Never do: force-push main"
    assert len(history.events(tmp_path)) == len(first) + 2


def test_added_lines_link_to_earlier_proposals(tmp_path):
    sync.init(tmp_path)
    history.snapshot(tmp_path, at=T0)
    cands = lessons.candidates(friction())
    history.record_proposals(tmp_path, cands, at=T0 + timedelta(minutes=1))
    assert history.record_proposals(tmp_path, cands) == []  # stored once
    lessons.add(tmp_path, "Use pnpm, not npm, for installing packages")
    lessons.add(tmp_path, "Write commit messages in English")
    e = history.snapshot(tmp_path, at=T0 + timedelta(minutes=5))
    linked, unlinked = e[0]["added"]
    assert linked["proposal"] == cands[0]["id"] and 0.25 <= linked["similarity"] <= 1
    assert unlinked["proposal"] is None
    s = history.summary(history.events(tmp_path))
    assert s["added_lines"] == 2 and s["from_proposals"] == 1


# ------------------------------------------------------------------- effect

def test_effect_four_cells_and_load_trigger(tmp_path):
    sync.init(tmp_path)
    for name in ("deploy", "testing", "styling", "docs"):
        skills.create(tmp_path, name, f"Use for {name}.")
    history.snapshot(tmp_path, at=T0)
    lessons.add(tmp_path, "Run migrations before deploying the staging server", skill="deploy")
    lessons.add(tmp_path, "Run vitest with the run flag, never watch mode", skill="testing")
    lessons.add(tmp_path, "Tailwind classes only, never inline styles", skill="styling")
    lessons.add(tmp_path, "Docstrings follow numpy convention", skill="docs")
    history.snapshot(tmp_path, at=T0 + timedelta(minutes=1))

    flags = detect.analyze([
        turn("Ship it: please use the deploy skill", "a", 0, 10),
        turn("Fix the flaky login test", "b", 0, 20),
        turn("No, vitest is still stuck in watch mode again", "b", 1, 25),
        turn("Restyle the header", "c", 0, 30),
        turn("Wrong: you used inline styles, I said tailwind", "c", 1, 35),
    ])
    tools = [tool("Skill", 11, "a", skill="deploy"),        # user named the skill
             tool("Skill", 21, "b", skill="testing")]       # agent chose it itself
    rows = {r["skill"]: r for r in effect.classify(effect.lessons(history.events(tmp_path)), flags, tools)}
    assert rows["deploy"]["cell"] == "effective" and rows["deploy"]["loads_user"] == 1
    assert rows["testing"]["cell"] == "ignored" and rows["testing"]["loads_auto"] == 1
    assert rows["styling"]["cell"] == "routing_failure"
    assert rows["docs"]["cell"] == "dormant"
    assert effect.table(list(rows.values())) == {"effective": 1, "ignored": 1, "routing_failure": 1, "dormant": 1}


def test_effect_respects_condition_window(tmp_path):
    sync.init(tmp_path)
    history.snapshot(tmp_path, at=T0)
    lessons.add(tmp_path, "Use pnpm never npm for packages")
    history.snapshot(tmp_path, at=T0 + timedelta(minutes=1))
    flags = detect.analyze([turn("x", "s", 0, 5), turn("No, use pnpm not npm", "s", 1, 10)])
    rows = effect.classify(effect.lessons(history.events(tmp_path)), flags, [])
    global_row = next(r for r in rows if "pnpm" in r["text"])
    assert global_row["cell"] == "ignored"  # always loaded, but it recurred
    rows = effect.classify(effect.lessons(history.events(tmp_path)), flags, [], in_condition=lambda ts: False)
    assert next(r for r in rows if "pnpm" in r["text"])["cell"] == "effective"


def test_load_counts_from_skill_and_read(tmp_path):
    c = effect.load_counts([tool("Skill", 1, skill="plugin:deploy"),
                            tool("Read", 2, file_path="/r/.claude/skills/deploy/SKILL.md"),
                            tool("Read", 3, file_path="/r/.context/skills/testing.md"),
                            tool("Read", 4, file_path="/r/src/app.py")])
    assert c == {"deploy": 2, "testing": 1}


def test_read_claude_tools(tmp_path):
    proj = tmp_path / "-proj"
    proj.mkdir()
    lines = [{"type": "assistant", "sessionId": "a", "timestamp": "2026-10-20T09:01:00Z",
              "message": {"content": [{"type": "text", "text": "ok"},
                                      {"type": "tool_use", "name": "Edit", "input": {"file_path": "/p/x.py"}}]}},
             {"type": "assistant", "sessionId": "a", "isSidechain": True,
              "message": {"content": [{"type": "tool_use", "name": "Read", "input": {}}]}}]
    (proj / "a.jsonl").write_text("\n".join(json.dumps(l) for l in lines))
    got = list(sources.read_claude_tools(tmp_path))
    assert [(t.name, t.input["file_path"]) for t in got] == [("Edit", "/p/x.py")]


# ------------------------------------------------------------------ metrics

def test_sessions_and_phases(tmp_path):
    phases = tmp_path / "phases.csv"
    phases.write_text("start,end,phase\n2026-10-19,2026-10-25,A\n")
    rows = metrics.sessions(friction(), metrics.load_phases(phases))
    s2 = next(r for r in rows if r["session"] == "s2")
    assert s2["turns"] == 3 and s2["corrections"] == 1 and s2["phase"] == "A" and s2["minutes"] == 2.0
    summary = metrics.summarize(rows)
    assert summary["A"]["corrections_per_100_turns"] == 40.0


def test_cold_start_from_first_edit():
    rows = metrics.sessions(friction(), tools=[tool("Read", 62, "s2"), tool("Edit", 63, "s2"), tool("Write", 64, "s2")])
    s2 = next(r for r in rows if r["session"] == "s2")
    s1 = next(r for r in rows if r["session"] == "s1")
    assert s2["cold_start_min"] == 3.0 and s1["cold_start_min"] == ""
    assert metrics.summarize(rows)["-"]["median_cold_start_min"] == 3.0


def test_label_and_score_roundtrip(tmp_path):
    rows = metrics.label_sample(friction(), n=4)
    for r in rows:  # pretend the LLM agrees with the detector
        r["llm_correction"], r["llm_repeat"] = r["det_correction"], r["det_repeat"]
    path = tmp_path / "labels.csv"
    metrics.write_csv(rows, path, metrics.LABEL_FIELDS)
    s = metrics.score(path)
    assert s["to_check"] == []
    assert s["correction"]["detector_vs_final"]["kappa"] == 1.0


def test_score_lists_disagreements_until_human_resolves(tmp_path):
    rows = metrics.label_sample(friction(), n=4)
    for r in rows:
        r["llm_correction"], r["llm_repeat"] = r["det_correction"], r["det_repeat"]
    flagged = next(r for r in rows if r["det_correction"] == 1)
    flagged["llm_correction"] = 0  # LLM disagrees with the detector here
    path = tmp_path / "labels.csv"
    metrics.write_csv(rows, path, metrics.LABEL_FIELDS)
    assert metrics.score(path)["to_check"] == [f"{flagged['id']} (correction)"]

    flagged["human_correction"] = 1  # you side with the detector
    metrics.write_csv(rows, path, metrics.LABEL_FIELDS)
    s = metrics.score(path)
    assert s["to_check"] == []
    assert s["correction"]["detector_vs_final"]["precision"] == 1.0
    assert s["correction"]["llm_vs_human"]["n"] == 1


# -------------------------------------------------------------------- study

from datetime import date  # noqa: E402

from context_user import study  # noqa: E402


def test_plan_rotates_all_conditions_on_weekdays_only():
    blocks = study.plan(date(2026, 10, 26), weeks=4, block_days=2, seed="p1")
    assert len(blocks) == 10
    for b in blocks:
        assert date.fromisoformat(b["start"]).weekday() < 5 and date.fromisoformat(b["end"]).weekday() < 5
    for i in range(0, 9, 3):  # every full cycle uses each condition exactly once
        assert sorted(b["phase"] for b in blocks[i:i + 3]) == sorted(study.CONDITIONS)
    assert blocks != study.plan(date(2026, 10, 26), seed="p2")  # order differs per participant
    assert blocks == study.plan(date(2026, 10, 26), seed="p1")  # and is reproducible


def test_apply_switches_conditions_without_losing_files(tmp_path):
    sync.init(tmp_path)
    (tmp_path / ".context/PROJECT.md").write_text("# P\n- Use pnpm\n")
    claude = tmp_path / "CLAUDE.md"

    study.apply(tmp_path, "context-user")
    assert sync.MARKER in claude.read_text()
    msgs = study.apply(tmp_path, "generated")  # first time: asks for /init
    assert not claude.exists() and "/init" in msgs[0]
    claude.write_text("# Generated by /init\nBuild with make.")  # participant runs /init
    study.apply(tmp_path, "generated")  # captures it
    assert (tmp_path / study.SAVED_GENERATED).exists()
    assert "Generated by /init" in (tmp_path / ".cursor/rules/generated.mdc").read_text()

    study.apply(tmp_path, "none")
    assert not claude.exists() and not (tmp_path / ".cursor/rules/generated.mdc").exists()
    study.apply(tmp_path, "generated")  # later blocks restore the saved file
    assert "Generated by /init" in claude.read_text()
    study.apply(tmp_path, "context-user")
    assert sync.MARKER in claude.read_text()


def test_apply_never_overwrites_own_claude_md(tmp_path):
    sync.init(tmp_path)
    (tmp_path / "CLAUDE.md").write_text("my own notes")
    assert "Warning" in study.apply(tmp_path, "none")[1]
    with pytest.raises(FileExistsError):
        study.apply(tmp_path, "context-user")
    assert (tmp_path / "CLAUDE.md").read_text() == "my own notes"


def test_export_contains_numbers_not_text(tmp_path):
    sync.init(tmp_path)
    study.init(tmp_path, "secret-name", date(2026, 10, 19), weeks=1, block_days=5)
    history.snapshot(tmp_path, at=T0)
    history.record_proposals(tmp_path, lessons.candidates(friction()), at=T0)
    lessons.add(tmp_path, "Use pnpm, not npm, for installing packages")
    lessons.add(tmp_path, "Commit messages in imperative mood")
    history.snapshot(tmp_path, at=T0 + timedelta(minutes=1))
    data = study.export(tmp_path, friction(), [])
    dump = json.dumps(data)
    for secret in ("pnpm", "imperative", "Build the API", "secret-name", "/proj"):
        assert secret not in dump
    assert data["effect_by_origin"]["proposed"] and sum(data["effect_by_origin"]["own"].values()) == 1
    assert {s["phase"] for s in data["sessions"]} <= set(study.CONDITIONS)


def test_removed_lessons_get_removal_time(tmp_path):
    sync.init(tmp_path)
    history.snapshot(tmp_path, at=T0)
    lessons.add(tmp_path, "Temporary rule about tabs")
    history.snapshot(tmp_path, at=T0 + timedelta(minutes=1))
    path = tmp_path / ".context/LESSONS.md"
    path.write_text(path.read_text().replace("- Temporary rule about tabs\n", ""))
    history.snapshot(tmp_path, at=T0 + timedelta(minutes=2))
    (lesson,) = effect.lessons(history.events(tmp_path))
    assert lesson["removed_at"] == T0 + timedelta(minutes=2)


# ---------------------------------------------------------------------- llm

from context_user import llm  # noqa: E402


def fake_runner(answers):
    """Stands in for `claude -p`: answers by message text."""
    calls = []

    def run(prompt, model):
        calls.append(prompt)
        items = json.loads(prompt[prompt.index("Items:") + len("Items:"):])
        return "Here you go:\n" + json.dumps([{"id": it["id"], **answers.get(it["dev"], {})} for it in items])
    run.calls = calls
    return run


def test_classify_caches_and_skips_interrupts(tmp_path):
    ts = [turn("Build the API", "s1", 0), turn("I think we should keep it simple", "s1", 1),
          Turn("claude", "s1", "/proj", T0, 2, "[Request interrupted by user]", kind="interrupt")]
    run = fake_runner({"I think we should keep it simple": {"correction": True, "lesson": "Keep it simple"}})
    cache = llm.classify(tmp_path, ts, runner=run, say=lambda *_: None)
    assert len(cache) == 2 and sum(r["correction"] for r in cache.values()) == 1
    assert llm.CLASSIFIER_MARKER in run.calls[0]
    llm.classify(tmp_path, ts, runner=run, say=lambda *_: None)
    assert len(run.calls) == 1  # second run: everything cached


def test_detector_modes_and_llm_lessons(tmp_path):
    ts = [turn("Build the API", "s1", 0),
          turn("usually I just keep asking, I rarely answer yes or no", "s1", 1),
          turn("No, use pnpm not npm", "s1", 2)]
    labels = llm.classify(tmp_path, ts, say=lambda *_: None, runner=fake_runner({
        "usually I just keep asking, I rarely answer yes or no":
            {"correction": True, "lesson": "Don't force accept/reject decisions"}}))
    rules = detect.analyze(ts, labels, "rules")
    only_llm = detect.analyze(ts, labels, "llm")
    union = detect.analyze(ts, labels, "union")
    assert [bool(f.correction) for f in rules] == [False, False, True]
    assert [bool(f.correction) for f in only_llm] == [False, True, False]
    assert [bool(f.correction) for f in union] == [False, True, True]
    texts = {c["text"] for c in lessons.candidates(union)}
    assert "Don't force accept/reject decisions" in texts  # drafted rule, not the raw quote


def test_auto_channel_gives_auto_origin(tmp_path):
    sync.init(tmp_path)
    history.snapshot(tmp_path, at=T0)
    lessons.add(tmp_path, "Keep answers short")
    history.snapshot(tmp_path, channel_hint="auto", at=T0 + timedelta(minutes=1))
    (lesson,) = effect.lessons(history.events(tmp_path))
    assert lesson["origin"] == "auto"
    assert set(effect.table_by_origin([dict(lesson, cell="dormant")])) == {"auto", "proposed", "own"}


def test_parser_keeps_reply_quotes_midturn_and_interrupts(tmp_path):
    proj = tmp_path / "-proj"
    proj.mkdir()
    lines = [
        {"type": "user", "sessionId": "a", "timestamp": "2026-10-20T09:00:00Z", "message": {"content": "Start"}},
        {"type": "assistant", "sessionId": "a", "message": {"content": [{"type": "text", "text": "Plan: hand-label 100 turns"}]}},
        {"type": "queue-operation", "operation": "enqueue", "sessionId": "a",
         "timestamp": "2026-10-20T09:01:00Z", "content": "also, if it fails that's still a result"},
        {"type": "user", "sessionId": "a", "timestamp": "2026-10-20T09:02:00Z",
         "message": {"content": "<!-- reply 1 -->\n> hand-label 100 turns\n\ntoo much"}},
        {"type": "queue-operation", "operation": "enqueue", "sessionId": "a",
         "timestamp": "2026-10-20T09:02:00Z", "content": "<!-- reply 1 -->\n> hand-label 100 turns\n\ntoo much"},
        {"type": "user", "sessionId": "a", "timestamp": "2026-10-20T09:03:00Z",
         "message": {"content": [{"type": "text", "text": "[Request interrupted by user]"}]}},
        {"type": "user", "sessionId": "a", "message": {"content": "<command-name>/clear</command-name>"}},
        {"type": "user", "sessionId": "a", "message": {"content": "[context-user-classifier] label these"}},
    ]
    (proj / "a.jsonl").write_text("\n".join(json.dumps(l) for l in lines))
    got = list(sources.read_claude(tmp_path))
    assert [(t.kind, t.text.splitlines()[-1]) for t in got] == [
        ("prompt", "Start"), ("midturn", "also, if it fails that's still a result"),
        ("prompt", "too much"), ("interrupt", "[Request interrupted by user]")]
    assert got[2].prev == "Plan: hand-label 100 turns"
    assert detect.find_correction(got[2]) == "too much"  # quoted agent text is ignored
    assert detect.find_correction(got[3]) == "interrupt"


def test_pasted_article_does_not_trigger_correction():
    article = "研究一下这个文章：\n\n" + ("写作的时候注意不要用破折号。" * 200) + "\n\n你怎么看？"
    assert detect.find_correction(turn(article)) is None


# ---------------------------------------------------------------- revisions

from context_user import revisions as rv  # noqa: E402
from context_user.detect import tokens  # noqa: E402


DRAFT = ("# Learning from Friction\n\nThe tool learns from corrections. Friction detection is the core idea. "
         "We compare three conditions over four weeks. Developers approve every lesson before it is stored. "
         "Results are reported per condition. Every session is logged locally and analysed after the study ends. Participants receive a short guide and a schedule.")
REVISED = ("# Teaching Coding Agents\n\nThe tool learns from corrections. Structure and accumulation are the core idea. "
           "We compare three conditions over four weeks. Developers teach in any way they like. "
           "Results are reported per condition. Every session is logged locally and analysed after the study ends. Participants receive a short guide and a schedule.")


def test_diff_ignores_formatting_and_finds_changed_sentences():
    removed, added = rv.diff(DRAFT, REVISED)
    assert "Friction detection is the core idea." in removed
    assert "Structure and accumulation are the core idea." in added
    assert not any("three conditions" in x for x in removed + added)  # unchanged
    assert rv.diff("**Bold** text here, ok.", "Bold text here, ok.") == ([], [])


def test_pasted_revision_found_and_unrelated_paste_ignored():
    drafts = [rv.Draft("s", T0, DRAFT, "/p/proposal.md", tokens(DRAFT))]
    turns = [turn(REVISED, "s", 3, 10), turn("Here is an unrelated article about cooking pasta. " * 10, "s", 4, 20)]
    (r,) = rv.pasted_revisions(turns, drafts)
    assert r.kind == "paste" and r.origin == "/p/proposal.md" and r.added
    assert rv.pasted_revisions([turn(REVISED, "s", 3, -10)], drafts) == []  # draft must come first


def test_file_revision_applies_agent_edits_and_skips_shell_touched(tmp_path):
    proj = tmp_path / "logs" / "-p"
    proj.mkdir(parents=True)
    doc, other = tmp_path / "plan.md", tmp_path / "notes.md"
    def a(ts, name, **inp):
        return {"type": "assistant", "sessionId": "s", "timestamp": ts,
                "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]}}
    lines = [a("2026-10-20T09:00:00Z", "Write", file_path=str(doc), content="Step one is setup.\nStep two is testing."),
             a("2026-10-20T09:01:00Z", "Edit", file_path=str(doc), old_string="testing", new_string="unit testing"),
             a("2026-10-20T09:02:00Z", "Write", file_path=str(other), content="Some notes written here."),
             a("2026-10-20T09:03:00Z", "Bash", command=f"sed -i 's/notes/text/' {other}")]
    (proj / "s.jsonl").write_text("\n".join(json.dumps(l) for l in lines))
    _, files = rv.read_drafts(tmp_path / "logs")
    assert set(files) == {str(doc)}  # notes.md was touched by a shell command: ambiguous, dropped
    assert files[str(doc)][2] == "Step one is setup.\nStep two is unit testing."
    doc.write_text("Step one is setup.\nStep two is unit testing.")
    assert rv.file_revisions(files) == []  # unchanged by the developer
    doc.write_text("Step one is environment setup.\nStep two is unit testing.")
    (r,) = rv.file_revisions(files)
    assert r.removed == ["Step one is setup."] and r.added == ["Step one is environment setup."]


def test_classify_revisions_and_suggest_uses_them(tmp_path):
    drafts = [rv.Draft("s", T0, DRAFT, "reply", tokens(DRAFT))]
    (r,) = rv.pasted_revisions([turn(REVISED, "s", 3, 10)], drafts)
    def run(prompt, model):
        assert rv.CLASSIFIER_MARKER in prompt
        return json.dumps([{"id": "0", "lessons": [{"lesson": "Lead with structure, not friction", "scope": "project"},
                                                    {"lesson": "Hedge novelty claims", "scope": "global"}]}])
    cache = rv.classify(tmp_path, [r], runner=run, say=lambda *_: None)
    assert [x["lesson"] for x in cache[r.key]["lessons"]] == ["Lead with structure, not friction", "Hedge novelty claims"]
    assert rv.classify(tmp_path, [r], runner=lambda *a: pytest.fail("cached"), say=lambda *_: None)


def test_read_chat_export(tmp_path):
    export = [{"uuid": "c1", "name": "Proposal", "chat_messages": [
        {"sender": "human", "text": "Draft my proposal", "created_at": "2026-10-20T09:00:00Z"},
        {"sender": "assistant", "content": [{"type": "text", "text": "Here is a draft."}], "created_at": "2026-10-20T09:01:00Z"},
        {"sender": "human", "text": "<!-- reply 1 -->\nNo, too long", "created_at": "2026-10-20T09:02:00Z"}]}]
    path = tmp_path / "conversations.json"
    path.write_text(json.dumps(export))
    turns, replies = sources.read_chat_export(path)
    assert [(t.source, t.index, t.text, t.prev) for t in turns] == [
        ("claude.ai", 0, "Draft my proposal", ""), ("claude.ai", 1, "No, too long", "Here is a draft.")]
    assert replies == [("c1", turns[1].ts.replace(minute=1), "Here is a draft.")]
    assert detect.find_correction(turns[1]) is not None


def test_recurring_mid_session_instruction_suggests_a_skill():
    ask = "check Alex's communication style and rewrite your delivery in that voice"
    ts = []
    for i, task in enumerate(["Polish my CV summary", "Draft a story for the interview bank", "Write a cover email"]):
        ts += [turn(task, f"s{i}", 0, i * 60), turn(ask + ("" if i else " please"), f"s{i}", 1, i * 60 + 5)]
    found = skills.recurring_tasks(detect.analyze(ts), [], min_sessions=3)
    assert any("Alex" in ex for g in found for ex in g["examples"]) and found[0]["sessions"] == 3


# -------------------------------------------------------------- learn/schedule

from context_user import learn, schedule  # noqa: E402


def labels_runner(prompt, model):
    """Fake classifier: corrections get a lesson; 'one-off' text gets scope one-off."""
    items = json.loads(prompt[prompt.index("Items:") + len("Items:"):])
    out = []
    for it in items:
        if "pnpm" in it["dev"]:
            out.append({"id": it["id"], "correction": True, "lesson": "Use pnpm, never npm", "scope": "global"})
        elif "rename" in it["dev"]:
            out.append({"id": it["id"], "correction": True, "lesson": "Call this file draft2", "scope": "one-off"})
        else:
            out.append({"id": it["id"]})
    return json.dumps(out)


def learn_turns():
    return [turn("Set up the repo", "s1", 0), turn("No, use pnpm not npm", "s1", 1),
            turn("rename it to draft2", "s1", 2)]


def test_learn_adds_reusable_lessons_only_once(tmp_path):
    sync.init(tmp_path)
    history.snapshot(tmp_path, at=T0)
    r = learn.learn(tmp_path, learn_turns(), [], runner=labels_runner, say=lambda *_: None)
    assert r["added"] == 1 and r["lessons"] == ["Use pnpm, never npm"]  # one-off skipped
    assert "- Use pnpm, never npm" in (tmp_path / ".context/LESSONS.md").read_text()
    assert sync.MARKER in (tmp_path / "CLAUDE.md").read_text()            # synced
    (lesson,) = [l for l in effect.lessons(history.events(tmp_path)) if "pnpm" in l["text"]]
    assert lesson["origin"] == "auto"
    assert learn.learn(tmp_path, learn_turns(), [], runner=labels_runner)["added"] == 0  # no duplicates
    log = (tmp_path / ".context/learn.log").read_text().splitlines()
    assert len(log) == 2 and json.loads(log[0])["added"] == 1


def test_learn_guards(tmp_path, monkeypatch):
    sync.init(tmp_path)
    (tmp_path / learn.LOCK).write_text("123")
    assert learn.learn(tmp_path, learn_turns(), [], runner=labels_runner) == {"skipped": "another round is running"}
    (tmp_path / learn.LOCK).unlink()
    monkeypatch.setenv("CU_LEARNING", "1")
    assert learn.learn(tmp_path, learn_turns(), [], runner=labels_runner) == {"skipped": "nested call"}


def test_learn_classifies_but_never_writes_outside_context_user_days(tmp_path):
    sync.init(tmp_path)
    cfg = study.init(tmp_path, "p", date.today(), weeks=1, block_days=5)
    cfg["blocks"][0]["phase"] = "none"
    (tmp_path / study.STUDY).write_text(json.dumps(cfg))
    r = learn.learn(tmp_path, learn_turns(), [], runner=labels_runner)
    if study.condition_on(cfg, date.today()) == "none":
        assert r["added"] == 0 and not r["writing"] and "none" in r["note"]
        assert (tmp_path / llm.CACHE).exists()  # still classified, ready for later


def test_hook_install_is_idempotent_and_keeps_other_hooks(tmp_path):
    settings = tmp_path / schedule.SETTINGS
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"hooks": {"SessionEnd": [{"hooks": [{"type": "command", "command": "echo bye"}]}]},
                                    "model": "sonnet"}))
    schedule.install_hook(tmp_path)
    schedule.install_hook(tmp_path)
    data = json.loads(settings.read_text())
    cmds = [h["command"] for g in data["hooks"]["SessionEnd"] for h in g["hooks"]]
    assert cmds[0] == "echo bye" and sum("cu learn" in c or " learn --quiet" in c for c in cmds) == 1
    assert data["model"] == "sonnet"
    assert schedule.remove_hook(tmp_path)
    data = json.loads(settings.read_text())
    assert [h["command"] for g in data["hooks"]["SessionEnd"] for h in g["hooks"]] == ["echo bye"]


def test_cron_line(tmp_path):
    line = schedule.cron_line(tmp_path, "daily")
    assert line.startswith("50 17 * * * cd ") and "learn --quiet" in line and f"# cu learn: {tmp_path}" in line


def test_quoting_a_reply_is_not_a_revision():
    reply = ("我把整个系统在 **我们这一周的真实对话** 上跑了一遍，发现了几个严重问题。主要的都已修复。" * 4
             + " It uses \"straight\" quotes and **bold** words. The parser now reads 33 messages instead of 25.")
    drafts = [rv.Draft("s", T0, reply, "reply", tokens(reply))]
    copied = "\n".join("> " + l for l in reply.replace("**", "").replace("\"", "“").splitlines()) + "\n\nthis matters"
    assert rv.pasted_revisions([turn(copied, "s", 3, 10)], drafts) == []
    assert rv.diff(reply, reply.replace("**", "").replace(" 上", "上").replace("\"", "“")) == ([], [])


def test_auto_add_caps_dedupes_and_scopes(tmp_path):
    sync.init(tmp_path)
    lessons.add(tmp_path, "Keep the .md version alongside each .docx deliverable")
    rules = ["Answer in short paragraphs", "Cite page numbers for papers", "Ask before heavy manual work",
             "Prefer tables for comparisons", "Explain reasoning behind scores", "Avoid buzzwords entirely",
             "Use metric units"]
    found = [{"id": str(i), "lesson": r, "scope": "global", "source": "x"} for i, r in enumerate(rules)]
    found.insert(0, {"id": "dup", "lesson": "Keep a .md version alongside every .docx deliverable",
                     "scope": "global", "source": "x"})
    found.insert(1, {"id": "proj", "lesson": "Frame this proposal around the developer as teacher",
                     "scope": "project", "source": "x"})
    added = learn.auto_add(tmp_path, found)
    assert len(added) == learn.MAX_PER_ROUND
    assert "dup" not in {a["id"] for a in added}  # near-duplicate of an existing line
    assert added[0]["id"] == "proj" and added[0]["home"] == "project"
    assert "developer as teacher" in (tmp_path / ".context/PROJECT.md").read_text()


def test_template_examples_are_for_people_only(tmp_path):
    sync.init(tmp_path)
    history.snapshot(tmp_path, at=T0)
    sync.sync(tmp_path)
    claude = (tmp_path / "CLAUDE.md").read_text()
    assert "Happy to help" not in claude and "<!--" not in claude.replace(sync.MARKER, "")
    assert "**Things that annoy me:**" in claude  # the structure itself stays
    assert effect.lessons(history.events(tmp_path)) == []  # examples are not lessons


# -------------------------------------------------------------------- infer

from context_user import infer  # noqa: E402


def test_read_chatgpt_export_follows_current_branch(tmp_path):
    def node(i, parent, role, text, children=()):
        return {"id": i, "parent": parent, "children": list(children),
                "message": None if role is None else {"author": {"role": role}, "create_time": 1792400000 + int(i[1:]),
                                                      "content": {"content_type": "text", "parts": [text]}}}
    conv = {"conversation_id": "g1", "current_node": "n4", "mapping": {
        "n0": node("n0", None, None, ""),
        "n1": node("n1", "n0", "user", "Write my bio", ["n2", "n3"]),
        "n2": node("n2", "n1", "assistant", "Old regenerated answer"),
        "n3": node("n3", "n1", "assistant", "Here is a bio.", ["n4"]),
        "n4": node("n4", "n3", "user", "Make it shorter"),
    }}
    path = tmp_path / "conversations.json"
    path.write_text(json.dumps([conv]))
    turns, replies = sources.read_chat_export(path)
    assert [(t.source, t.text, t.prev) for t in turns] == [
        ("chatgpt", "Write my bio", ""), ("chatgpt", "Make it shorter", "Here is a bio.")]
    assert [r[2] for r in replies] == ["Here is a bio."]  # the regenerated branch is ignored


def refine_flags():
    ts = []
    for i in range(3):
        ts += [turn(f"Draft email number {i}", f"c{i}", 0, i * 60), turn("make it shorter and plainer please", f"c{i}", 1, i * 60 + 5)]
    ts += [turn("Fix the CSS", "c9", 0, 500), turn("rename the file to final.docx", "c9", 1, 505)]
    return ts


def fake_labels(tmp_path, ts):
    def run(prompt, model):
        items = json.loads(prompt[prompt.index("Items:") + len("Items:"):])
        return json.dumps([{"id": it["id"], "refine": it["dev"] != it["dev"].split()[0] and "Draft" not in it["dev"]
                            and "Fix" not in it["dev"],
                            "lesson": "Keep it short and plain" if "shorter" in it["dev"] else "Name it final.docx",
                            "scope": "global"} for it in items])
    return llm.classify(tmp_path, ts, runner=run, say=lambda *_: None)


def test_infer_requires_recurrence_across_conversations(tmp_path):
    ts = refine_flags()
    labels = fake_labels(tmp_path, ts)
    refs = infer.refinements(detect.analyze(ts, labels, "union"), labels)
    assert len(refs) == 4
    ids = {r["text"]: r["id"] for r in refs}
    def run(prompt, model):
        shorter = [r["id"] for r in refs if "shorter" in r["text"]]
        return json.dumps([{"name": "Plain Short Writing", "title": "Short, plain writing", "when": "drafting emails",
                            "instructions": ["Keep it short", "Use plain words"], "domain": "writing",
                            "evidence": shorter + ["not-a-real-id"]},
                           {"name": "file-names", "title": "File names", "instructions": ["Name it final.docx"],
                            "evidence": [ids["rename the file to final.docx"]]}])
    found = infer.infer(refs, n=10, min_conv=2, runner=run)
    assert [s["name"] for s in found] == ["plain-short-writing"]  # the one-off is dropped
    assert found[0]["conversations"] == 3 and "not-a-real-id" not in found[0]["evidence"]


def test_write_rate_apply_and_summary(tmp_path):
    sync.init(tmp_path)
    ts = refine_flags()
    labels = fake_labels(tmp_path, ts)
    refs = infer.refinements(detect.analyze(ts, labels, "union"), labels)
    found = infer.infer(refs, runner=False)  # word-overlap fallback, no LLM
    assert found and found[0]["conversations"] == 3
    paths = infer.write(tmp_path, found, refs)
    assert "make it shorter" in paths["md"].read_text()           # quotes stay in the local file
    assert "make it shorter" not in paths["ratings"].read_text()  # but never in the shared sheet
    answers = iter(["7", "5", "I always ask for this"])
    infer.rate(tmp_path, ask=lambda _: next(answers), say=lambda *_: None)
    rated = list(infer.ratings(tmp_path).values())
    assert rated[0]["rating_1_to_5"] == "5" and rated[0]["why"] == "I always ask for this"
    assert infer.summary(rated)["all"] == {"n": 1, "mean": 5.0, "share_4_or_5": 1.0}
    (written,) = infer.apply(tmp_path)
    assert "Keep it short and plain" in written.read_text()


def test_compare_before_after(tmp_path):
    ts = refine_flags()
    labels = fake_labels(tmp_path, ts)
    flags = detect.analyze(ts, labels, "union")
    skill = [{"title": "Short plain writing", "instructions": ["Keep it short and plain"]}]
    out = infer.compare(flags, labels, T0 + timedelta(minutes=100), skill)
    assert out["before"]["messages"] == 4 and out["before"]["covered_refinements_per_100"] == 50.0
    assert out["after"]["conversations"] == 2
