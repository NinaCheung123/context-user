"""cu — Context-User command line.

Teach
  cu init                 create .context/ (USER, PROJECT, LESSONS, skills/)
  cu sync                 write Claude Code + Cursor native files from .context/
  cu add "text" [--skill NAME]   add a lesson (global, or a skill's known pitfall)
  cu suggest              lesson proposals from your corrections and repeats
  cu classify [--auto-add]  label friction with Claude (headless); optionally write lessons automatically
  cu learn                one automatic round: classify, add reusable lessons, sync, log
  cu schedule hook|cron   run `cu learn` after every Claude Code session, or daily/hourly
  cu revisions [--classify]  how you revised the agent's drafts, and what that teaches
  cu suggest-skills       recurring kinds of task that deserve a skill
  cu skills               list skills
  cu clean                remove all generated files (the "no context" condition)

Infer skills from your history (the study)
  cu infer [--top 10]     skills from refinements you repeat across conversations
  cu rate                 rate each inferred skill: value 1–5 and why → ratings.csv
  cu apply [--min-rating 4]   put well-rated skills into use (then cu sync)
  cu compare --since DATE     before/after: repeated refinements per 100 messages

Measure
  cu snapshot             record changes to .context/ (every command also does this)
  cu history              teaching events by week and channel, proposal adoption
  cu effect               per lesson: effective / ignored / routing failure / dormant
  cu metrics              per-session friction, cold start, length; per-condition summary
  cu label-sample / cu score labels.csv   validate the friction detector
  cu doctor               show which logs were found

Study (for participants)
  cu study init --participant NAME [--start YYYY-MM-DD]   personal block schedule
  cu study today          show and apply today's condition
  cu study export         anonymized numbers to send back (no prompts, no lesson text)
  cu study run            two blind skill sets (direct vs method) from your history, on your computer
  cu study answers        answer a few of your recent tasks under three conditions, shuffled
  cu study page           build the local comparison page (open the file in a browser)
  cu study decode FILE    turn the page's choices.json into ratings.csv to send back
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from datetime import date, datetime, timezone

import os

from . import detect, effect, history, infer, learn, lessons, llm, metrics, pipeline, revisions, schedule, skills, sources, study, studypage, sync


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="cu", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", type=Path, default=Path.cwd(), help="repo containing .context/")
    p.add_argument("--project", help="only logs whose working dir contains this text")
    p.add_argument("--sources", default="claude,cursor")
    p.add_argument("--chat-export", type=Path, action="append",
                   help="claude.ai or ChatGPT export (conversations.json); repeat for several")
    p.add_argument("--detector", choices=["auto", "rules", "llm", "union"], default="auto",
                   help="friction detector; auto = union if `cu classify` labels exist, else rules")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init")
    s = sub.add_parser("sync")
    s.add_argument("--targets", default="claude,cursor", help="claude,cursor,agents")
    s.add_argument("--force", action="store_true")
    a = sub.add_parser("add")
    a.add_argument("text")
    a.add_argument("--skill", help="skill name; omit for a global lesson")
    sg = sub.add_parser("suggest")
    sg.add_argument("--min-count", type=int, default=2, help="only patterns seen this often")
    cl = sub.add_parser("classify")
    cl.add_argument("--model", default="haiku")
    cl.add_argument("--auto-add", action="store_true",
                    help="write every drafted lesson into .context/ without asking (the 'auto' condition)")
    inf = sub.add_parser("infer")
    inf.add_argument("--top", type=int, default=10)
    inf.add_argument("--min-conversations", type=int, default=2)
    inf.add_argument("--model", default="sonnet", help="model for grouping refinements into skills")
    inf.add_argument("--label-model", default="haiku", help="model for labelling messages")
    inf.add_argument("--no-llm", action="store_true", help="keyword rules and word overlap only")
    sub.add_parser("rate")
    ap = sub.add_parser("apply")
    ap.add_argument("--min-rating", type=int, default=4)
    cmp_ = sub.add_parser("compare")
    cmp_.add_argument("--since", required=True, help="date the skills were applied, YYYY-MM-DD")
    le = sub.add_parser("learn")
    le.add_argument("--model", default="haiku")
    le.add_argument("--no-write", action="store_true", help="only classify; write nothing")
    le.add_argument("--quiet", action="store_true")
    sch = sub.add_parser("schedule")
    schsub = sch.add_subparsers(dest="schedule_cmd", required=True)
    sh = schsub.add_parser("hook", help="SessionEnd hook in .claude/settings.local.json")
    sh.add_argument("--remove", action="store_true")
    sc_ = schsub.add_parser("cron", help="crontab line (printed; --install to add it)")
    sc_.add_argument("--every", choices=sorted(schedule.SCHEDULES), default="daily")
    sc_.add_argument("--install", action="store_true")
    sc_.add_argument("--remove", action="store_true")
    rv = sub.add_parser("revisions")
    rv.add_argument("--classify", action="store_true", help="ask Claude what each revision teaches")
    rv.add_argument("--model", default="haiku")
    ss = sub.add_parser("suggest-skills")
    ss.add_argument("--min-sessions", type=int, default=3)
    sub.add_parser("skills")
    sub.add_parser("clean")
    sub.add_parser("snapshot")
    sub.add_parser("history")
    ef = sub.add_parser("effect")
    ef.add_argument("--phases", type=Path, help="CSV with start,end,phase")
    ef.add_argument("--condition", help="only count evidence from this phase label (e.g. Context-User)")
    ef.add_argument("--out", type=Path, help="write per-lesson CSV")
    m = sub.add_parser("metrics")
    m.add_argument("--phases", type=Path, help="CSV with start,end,phase")
    m.add_argument("--out", type=Path, default=Path("sessions.csv"))
    ls = sub.add_parser("label-sample")
    ls.add_argument("--n", type=int, default=100)
    ls.add_argument("--out", type=Path, default=Path("labels.csv"))
    sc = sub.add_parser("score")
    sc.add_argument("labels", type=Path)
    sub.add_parser("doctor")
    st = sub.add_parser("study")
    stsub = st.add_subparsers(dest="study_cmd", required=True)
    si = stsub.add_parser("init")
    si.add_argument("--participant", required=True, help="a pseudonym, not your real name")
    si.add_argument("--start", type=date.fromisoformat, default=date.today())
    si.add_argument("--weeks", type=int, default=4)
    si.add_argument("--block-days", type=int, default=2)
    stsub.add_parser("today")
    sa = stsub.add_parser("answers", help="answer N of your recent tasks under three conditions, shuffled")
    sa.add_argument("--n", type=int, default=5)
    stsub.add_parser("page", help="build the local comparison page")
    sd = stsub.add_parser("decode", help="turn choices.json into ratings.csv (letters to conditions, on your computer)")
    sd.add_argument("choices", type=Path)
    sd.add_argument("--out", type=Path, default=Path("ratings.csv"))
    sr = stsub.add_parser("run", help="make two blind skill sets from your history (runs on your computer)")
    sr.add_argument("--holdout-days", type=int, default=14, help="latest N days kept aside for answer comparison")
    sr.add_argument("--since", help="only messages from this date (YYYY-MM-DD)")
    sr.add_argument("--until", help="only messages up to this date (YYYY-MM-DD)")
    sr.add_argument("--max-messages", type=int, default=300, help="cap on messages sent to the model (signals first, then a random sample)")
    sr.add_argument("--limit", type=int, default=None, help="use only the latest N training messages (quick test)")
    se = stsub.add_parser("export")
    se.add_argument("--out", type=Path, default=Path("context_user_export.json"))

    args = p.parse_args(argv)
    root = args.root
    if args.cmd == "learn" and os.environ.get("CU_LEARNING"):
        return  # started from the classifier's own session: never loop
    srcs = args.sources.split(",")
    _cache: dict = {}

    def tools():
        if "tools" not in _cache:
            _cache["tools"] = list(sources.read_claude_tools()) if "claude" in srcs else []
        return _cache["tools"]

    def turns():
        if "turns" not in _cache:
            _cache["turns"] = sources.read_all(project=args.project, sources=srcs, chat_export=args.chat_export)
        return _cache["turns"]

    def flags():
        if "flags" not in _cache:
            labels = llm.load_cache(root)
            mode = args.detector if args.detector != "auto" else ("union" if labels else "rules")
            _cache["mode"] = mode
            _cache["flags"] = detect.analyze(turns(), labels, mode)
        return _cache["flags"]

    def revs():
        if "revs" not in _cache:
            drafts, files = revisions.read_drafts() if "claude" in srcs else ([], {})
            for path in args.chat_export or []:
                drafts += revisions.chat_drafts(sources.read_chat_export(path)[1])
            _cache["revs"] = revisions.pasted_revisions(turns(), drafts) + revisions.file_revisions(files)
        return _cache["revs"]

    def snap(hint=None):
        return history.snapshot(root, tools(), channel_hint=hint)

    if args.cmd not in ("init", "doctor", "score", "label-sample", "schedule"):
        _cache["last"] = snap()

    if args.cmd == "init":
        for path in sync.init(root):
            print("created", path)
        snap()
        print("Fill in .context/USER.md and PROJECT.md, then run `cu sync`.")
    elif args.cmd == "sync":
        for path in sync.sync(root, args.targets.split(","), args.force):
            print("wrote", path)
    elif args.cmd == "add":
        path = lessons.add(root, args.text, args.skill)
        snap("cli")
        print(f"added to {path.relative_to(root)}. Run `cu sync` to push it to your tools.")
    elif args.cmd == "suggest":
        cands = lessons.candidates(flags(), args.min_count)
        rev_labels = revisions.load_cache(root)
        for r in revs():  # lessons drawn from how you revised the agent's drafts
            for les in (rev_labels.get(r.key) or {}).get("lessons", []):
                if les["scope"] != "one-off":
                    cands.append({"id": lessons._id(les["lesson"]), "text": les["lesson"], "count": 1, "kind": "revision",
                                  "examples": [f"you revised a draft ({r.kind}): -{len(r.removed)} +{len(r.added)} sentences"]})
        history.record_proposals(root, cands)
        if not cands:
            print("No repeated friction yet.")
        for c in cands[:15]:
            print(f"\n{c['kind']} x{c['count']}  → likely home: {lessons.place(root, c['text'])}")
            if c["text"] != c["examples"][0]:
                print("   rule:", c["text"])
            for ex in c["examples"][:3]:
                print("   you said >", ex[:160])
        if cands:
            print("\nTeach however you like: edit .context/, ask your agent, or `cu add \"rule\" --skill NAME`.")
    elif args.cmd == "classify":
        cache = llm.classify(root, turns(), args.model)
        print(f"{sum(1 for r in cache.values() if r['correction'])} corrections, "
              f"{sum(1 for r in cache.values() if r['repeat'])} repeats among {len(cache)} labelled messages")
        if args.auto_add:
            added = learn.auto_add(root, learn.collect(cache, revisions.load_cache(root)))
            snap("auto")
            print(f"auto-added {len(added)} lesson(s). Run `cu sync` to push them to your tools.")
    elif args.cmd == "infer":
        labels = {} if args.no_llm else llm.classify(root, turns(), args.label_model, say=lambda *_: None)
        flags_ = detect.analyze(turns(), labels, "rules" if args.no_llm else "union")
        refs = infer.refinements(flags_, labels)
        convs = len({r["conversation"] for r in refs})
        print(f"{len(turns())} messages, {len(refs)} refinements in {convs} conversations")
        found = infer.infer(refs, args.top, args.min_conversations, args.model,
                            runner=False if args.no_llm else None)
        paths = infer.write(root, found, refs)
        for i, s in enumerate(found, 1):
            print(f"{i:2}. {s['title']}  [{s['domain']}, {s['conversations']} conversations]")
        if not found:
            print(f"No skill recurs in {args.min_conversations}+ conversations yet.")
        print(f"\nDetails with your quotes (stays local): {paths['md']}\nNext: `cu rate`, then share {paths['ratings']}")
    elif args.cmd == "rate":
        path = infer.rate(root)
        print(f"saved {path}")
        print(json.dumps(infer.summary(list(infer.ratings(root).values())), indent=2))
    elif args.cmd == "apply":
        for p_ in infer.apply(root, args.min_rating):
            print("added", p_.relative_to(root))
        print("Run `cu sync` to load them into Claude Code and Cursor.")
    elif args.cmd == "compare":
        since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
        labels = llm.load_cache(root)
        print(json.dumps(infer.compare(detect.analyze(turns(), labels, "union" if labels else "rules"),
                                       labels, since, infer.load(root)), indent=2))
    elif args.cmd == "learn":
        result = learn.learn(root, turns(), revs(), tools(), args.model, write=not args.no_write)
        if args.quiet:
            return
        if "skipped" in result:
            print("skipped:", result["skipped"])
        else:
            print(f"{result['messages']} messages, {result['revisions']} revisions classified; "
                  f"{result['added']} lesson(s) added ({result['note']})")
            for l in result["lessons"]:
                print("  +", l)
    elif args.cmd == "schedule":
        if args.schedule_cmd == "hook":
            if args.remove:
                print("hook removed" if schedule.remove_hook(root) else "no hook found")
            else:
                path = schedule.install_hook(root)
                print(f"SessionEnd hook written to {path}. After each Claude Code session in this project, "
                      "`cu learn` runs in the background; see .context/learn.log.")
        else:
            if args.remove:
                print("cron entry removed" if schedule.remove_cron(root) else "no cron entry found")
            elif args.install:
                print("added to crontab:\n ", schedule.install_cron(root, args.every))
            else:
                print("Add this line with `crontab -e`, or re-run with --install:\n ", schedule.cron_line(root, args.every))
    elif args.cmd == "revisions":
        found = revs()
        labels = revisions.classify(root, found, args.model) if args.classify and found else revisions.load_cache(root)
        if not found:
            print("No revised drafts found.")
        for r in found:
            where = "an earlier reply" if r.origin == "reply" else Path(r.origin).name
            print(f"\n{r.kind}: you revised {where} "
                  f"(similarity {r.similarity}, -{len(r.removed)} +{len(r.added)} sentences)")
            for x in r.removed[:2]:
                print("   -", x[:120])
            for x in r.added[:2]:
                print("   +", x[:120])
            lab = labels.get(r.key)
            if lab:
                for les in lab.get("lessons", []) or [{"lesson": "(none: content-only change)", "scope": "-"}]:
                    print(f"   lesson ({les['scope']}):", les["lesson"])
        if found and not args.classify and not labels:
            print("\nRun `cu revisions --classify` to ask Claude what these edits teach.")
    elif args.cmd == "suggest-skills":
        found = skills.recurring_tasks(flags(), skills.load(root), args.min_sessions)
        for g in found:
            print(f"\nStarted in {g['sessions']} sessions, e.g.:")
            for ex in g["examples"][:3]:
                print("   >", ex)
        if found:
            print("\nTo turn one into a skill, ask your agent: \"Create .context/skills/<name>.md for this "
                  "recurring task with Goal, Acceptance criteria, Resources & boundaries, and an empty "
                  "Known pitfalls section; then run cu sync.\"")
        else:
            print("No recurring tasks without a skill yet.")
    elif args.cmd == "skills":
        loads = effect.load_counts(tools())
        for s in skills.load(root):
            print(f"{s.name:24} loads={loads.get(s.name, 0):<4} {s.description}")
    elif args.cmd == "clean":
        for path in sync.clean(root):
            print("removed", path)
    elif args.cmd == "snapshot":
        evts = _cache["last"]
        print(f"recorded {len(evts)} teaching event(s)" if evts else "no changes since the last snapshot")
    elif args.cmd == "history":
        print(json.dumps(history.summary(history.events(root)), indent=2))
    elif args.cmd == "effect":
        phases = metrics.load_phases(args.phases)

        def in_condition(ts):
            return not args.condition or metrics.phase_of(ts.date(), phases) == args.condition

        rows = effect.classify(effect.lessons(history.events(root)), flags(), tools(), in_condition)
        if args.out:
            fields = ["file", "skill", "text", "channel", "added_at", "removed_at",
                      "loads_auto", "loads_user", "recurrences", "cell"]
            metrics.write_csv([{k: r[k] for k in fields} for r in rows], args.out, fields)
        print(json.dumps({"all": effect.table(rows), **effect.table_by_origin(rows)}, indent=2))
    elif args.cmd == "metrics":
        rows = metrics.sessions(flags(), metrics.load_phases(args.phases), tools())
        metrics.write_csv(rows, args.out)
        print(f"wrote {len(rows)} sessions to {args.out}")
        print(json.dumps(metrics.summarize(rows), indent=2))
    elif args.cmd == "label-sample":
        rows = metrics.label_sample(flags(), args.n)
        metrics.write_csv(rows, args.out, metrics.LABEL_FIELDS)
        print(f"wrote {len(rows)} rows to {args.out}; see README for the labelling steps")
    elif args.cmd == "score":
        print(json.dumps(metrics.score(args.labels), indent=2))
    elif args.cmd == "study":
        if args.study_cmd == "init":
            cfg = study.init(root, args.participant, args.start, args.weeks, args.block_days)
            for b in cfg["blocks"]:
                print(f"{b['start']} – {b['end']}  {b['phase']}")
            print("\nEach working day, run `cu study today` once before you start.")
        elif args.study_cmd == "run":
            counts = pipeline.run(turns(), root, args.holdout_days, args.limit, since=args.since, until=args.until, max_messages=args.max_messages)
            print(json.dumps(counts, indent=2))
            print(f"\nBlind skill sets are in {root / pipeline.OUT}. Nothing was uploaded.")
        elif args.study_cmd == "answers":
            print(f"{pipeline.answers(root, args.n)} tasks answered. Next: cu study page")
        elif args.study_cmd == "page":
            print(f"open this file in your browser: {studypage.build(root, root / pipeline.OUT)}")
        elif args.study_cmd == "decode":
            print(f"wrote {studypage.decode(root / pipeline.OUT, args.choices, args.out)}. Open it and check it before sending.")
        elif args.study_cmd == "today":
            cond = study.condition_on(study.load(root), date.today())
            if cond is None:
                print("No study block today (weekend, or outside the schedule).")
            else:
                print(f"Today's condition: {cond}")
                for line in study.apply(root, cond):
                    print(" ", line)
        elif args.study_cmd == "export":
            data = study.export(root, flags(), tools(), _cache.get("mode", "rules"))
            args.out.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"wrote {args.out}. Open it and check it before sending: it should contain numbers, dates "
                  "and hashes only.")
    elif args.cmd == "doctor":
        turns = sources.read_all(project=args.project, sources=srcs, chat_export=args.chat_export)
        print("Claude Code logs:", sources.CLAUDE_DIR, "exists" if sources.CLAUDE_DIR.exists() else "MISSING")
        print("Cursor store:    ", sources.CURSOR_DIR / "globalStorage/state.vscdb")
        if args.chat_export:
            print("chat exports:     ", ", ".join(str(x) for x in args.chat_export))
        by = Counter(t.source for t in turns)
        sess = Counter(t.source for t in {(t.source, t.session): t for t in turns}.values())
        for src in ("claude", "cursor", "claude.ai", "chatgpt"):
            print(f"  {src:9} {by[src]:6} prompts in {sess[src]:4} sessions")
        print(f"  claude  {len(tools()):6} agent tool calls")
        dated = [t.ts for t in turns if t.ts]
        if dated:
            print("  range:", min(dated).date(), "to", max(dated).date())


if __name__ == "__main__":
    main()
