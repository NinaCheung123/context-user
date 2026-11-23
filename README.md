# Context-User (`cu`)

Your AI agents forget everything between sessions. Context-User gives your teaching a **structure** (a session router and a skill schema) that **accumulates** across sessions, syncs it to Claude Code and Cursor in their native formats, and measures whether the teaching takes effect.

```
 sessions (Claude Code, Cursor)
        │  you teach any way you like: edit files, ask your agent, `cu add`
        │  (cu suggest proposes lessons from your corrections & repeats)
        ▼
 .context/                         ← one source of truth (you edit this)
 ├─ USER.md  PROJECT.md            always loaded with a generated skill index (session router)
 ├─ LESSONS.md                     global lessons
 └─ skills/<name>.md               Goal · Acceptance criteria · Resources · Known pitfalls
        │  cu sync
        ▼
 CLAUDE.md + .claude/skills/<name>/SKILL.md          (Claude Code loads skills on demand)
 .cursor/rules/context-user.mdc + skill-<name>.mdc     (Cursor: always + agent-requested)
 AGENTS.md with a generated skill index              (Codex and others, optional)
```

**Design principles**
- **Structure + accumulation.** Every skill has a goal, acceptance criteria, resources, and known pitfalls taken from real use, never guessed in advance.
- **Teach the way you already work.** No accept/reject buttons: Context-User snapshots `.context/` and records every change as a teaching event, whatever channel it came from.
- **Small always-on context, skills on demand.** Task-specific knowledge lives in a skill and loads only when relevant.
- **Local and read-only.** It only reads the tools' logs. Python 3.10+, no dependencies.

**Running a study with other developers?** Send them [PARTICIPANTS.md](PARTICIPANTS.md): `cu study init / today / export` handles the schedule, condition switching and an anonymized export.

## Install

Needs Python 3.10+ (macOS: `python3 --version`; if missing, install from python.org or `brew install python`).

```bash
pipx install git+https://github.com/ninacheung123/context-user.git      # recommended (brew install pipx, if you don't have it)
# or: python3 -m pip install --user git+https://github.com/ninacheung123/context-user.git
cu doctor                     # shows which Claude Code / Cursor logs it can read
```

Nothing runs in the background after installing; `cu` only does something when you call it (or when you set up `cu schedule`). Everything stays on your machine.

For development: `git clone https://github.com/ninacheung123/context-user.git && cd context-user && pip install -e ".[test]" && pytest -q`.

If `cu doctor` shows **0 Cursor prompts** even though you use Cursor, the storage format may have changed. Please open an issue with the output of:
`sqlite3 ~/Library/Application\ Support/Cursor/User/globalStorage/state.vscdb "SELECT key FROM cursorDiskKV LIMIT 20"`

## Daily use

```bash
cd ~/code/my-project
cu init                         # once: creates .context/
cu sync                         # write native files for Claude Code + Cursor
cu classify                     # optional: Claude labels your corrections and drafts rules (cached; ~cents)
cu revisions --classify        # optional: what your edits to the agent's drafts teach
cu suggest                      # lesson ideas from your corrections, repeats & revisions (optional)
cu add "Use pnpm, never npm" --skill dependencies    # or edit files / ask your agent
cu sync                         # push changes to your tools
cu suggest-skills               # tasks you keep starting from scratch → candidates for a skill
cu skills                       # skills and how often Claude Code loaded each
```

`cu sync` refuses to overwrite a `CLAUDE.md` you wrote by hand. Move its content into `.context/PROJECT.md` first, or pass `--force`.

## What gets measured

| Command | Output |
|---|---|
| `cu history` | **Teaching events**: lines added/removed per week, by channel (`conversation` = an agent edited `.context/` in a session, `manual`, `cli`), and how closely added text matches an earlier `cu suggest` proposal (1 = adopted as-is, lower = rewritten). |
| `cu effect` | Split into **proposed** (learned from your corrections via `cu suggest`) vs **own** (written unprompted) lessons. Every lesson goes in one of four cells: **effective** (loaded, mistake did not recur), **ignored** (loaded, recurred), **routing failure** (not loaded, recurred), **dormant** (neither). Loads are split into **auto** (agent chose the skill) and **user** (your prompt named it). |
| `cu metrics` | Per session: corrections, repeats, turns, minutes, **cold start** (first prompt to first file edit, Claude Code only). Summary per condition. |

**Sources.** Claude Code logs (`~/.claude/projects`), Cursor's local database, and, with `--chat-export conversations.json`, a claude.ai data export (Settings → Privacy → Export data). Messages you send while the agent is working, replies that quote the agent, and interruptions are all included.

**Revisions.** `cu revisions` finds the agent's drafts you changed: a message that is an edited version of an earlier agent reply or file (*paste*), or a file the agent wrote that you later edited (*file*; files the agent also touched through shell commands are skipped, since their later changes can't be attributed). `--classify` asks Claude for up to three lessons per revision, each scoped global / project / one-off; non-one-off lessons appear in `cu suggest`.

**Detector.** By default, keyword rules find corrections (fast, free, reproducible, but they miss most pushback in design discussions). `cu classify` adds a Claude-based classifier that sees what the agent said before each message and drafts a one-line rule; it runs through your own `claude` CLI with `--no-session-persistence`, and results are cached in `.context/llm_labels.jsonl`. With labels present, all commands use the union of both (`--detector rules|llm|union` to choose). `cu classify --auto-add` writes every drafted rule into `.context/` without asking: the **auto** condition, reported separately in `cu effect`.

**Automatic learning.** `cu learn` runs one round: classify new messages and revised drafts, write up to 5 reusable lessons (one-off decisions and near-duplicates are skipped; project decisions go to `PROJECT.md`), sync, and log to `.context/learn.log`. Run it automatically:

```bash
cu schedule hook                 # after every Claude Code session in this project (background, SessionEnd hook)
cu schedule cron --every daily   # or on a schedule; prints a crontab line, add --install to apply
cu schedule hook --remove        # undo
```

Safeguards: the classifier's own `claude -p` calls can never re-trigger the hook; two rounds never run at once; and while a study is running, rounds on "none" / "generated" days only classify and never write. Lessons written this way count as **auto** in `cu effect`.

Every `cu` command first snapshots `.context/`. For finer timing, run `cu snapshot` from a Claude Code hook or cron. Skill loads are only observable in Claude Code logs, not Cursor.

## Study protocol

**Conditions**, rotated in short blocks (e.g., 2 working days each, repeated A–B–C cycles):

| Condition | Command |
|---|---|
| **None** | `cu clean` (removes all generated files) |
| **Generated** | `cu clean`, then run `/init` in Claude Code and copy the result to `.cursor/rules/` |
| **context-user** | `cu sync --force` |

Log each block's condition in `phases.csv` (`start,end,phase`, one row per block), then run:

```bash
cu metrics --phases phases.csv --out sessions.csv
```

That gives the RQ3 outcomes per session and per condition. For RQ2, run `cu history` and `cu effect --phases phases.csv --condition context-user --out lessons.csv`.

**Validating the detector (≈10 minutes, once, before the study)**
1. Run `cu label-sample --n 100`.
2. In Claude Code, paste:
   > Read labels.csv. For each row, fill `llm_correction` with 1 if the text pushes back on or corrects what the agent just did, else 0. Fill `llm_repeat` with 1 if it re-states an instruction the developer has likely given an agent before, else 0. Judge from `text` only and ignore the `det_` columns. Do not change other columns.
3. Run `cu score labels.csv`, fill `human_*` for the listed disagreements only, then run `cu score` again.

**Privacy:** `sessions.csv`, `labels.csv` and `lessons.csv` contain your prompts and are git-ignored here; `.context/*.jsonl` in your projects does too, so keep it out of public repos. Anonymize everything before submitting.

## Acknowledgements

The profile template's structure and some of its examples are adapted from [grapeot/context-infrastructure](https://github.com/grapeot/context-infrastructure) (MIT).
