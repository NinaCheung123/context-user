"""Run `cu learn` automatically: after every Claude Code session, or on a schedule.

  hook  a SessionEnd hook in the project's .claude/settings.local.json (local,
        not committed: it contains this machine's paths). Runs in the
        background, so closing a session is never slowed down.
  cron  a crontab line (daily or hourly). Printed by default; --install adds
        it to your crontab.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

SETTINGS = Path(".claude/settings.local.json")
TAG = "cu learn"  # identifies our entries when updating or removing


def _cu() -> str:
    return shutil.which("cu") or "cu"


def hook_command(root: Path) -> str:
    log = shlex.quote(str(root / ".context/learn.log"))
    return (f"cd {shlex.quote(str(root))} && (nohup {shlex.quote(_cu())} learn --quiet "
            f">> {log} 2>&1 &)")


def install_hook(root: Path) -> Path:
    path = root / SETTINGS
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    groups = data.setdefault("hooks", {}).setdefault("SessionEnd", [])
    for g in groups:  # replace an older entry of ours instead of adding a second one
        g["hooks"] = [h for h in g.get("hooks", []) if TAG not in h.get("command", "")]
    groups[:] = [g for g in groups if g.get("hooks")]
    groups.append({"hooks": [{"type": "command", "command": hook_command(root)}]})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def remove_hook(root: Path) -> bool:
    path = root / SETTINGS
    if not path.exists():
        return False
    data = json.loads(path.read_text(encoding="utf-8"))
    hooks = data.get("hooks") or {}
    groups = hooks.get("SessionEnd") or []
    removed = False
    for g in groups:
        kept = [h for h in g.get("hooks", []) if TAG not in h.get("command", "")]
        removed |= len(kept) != len(g.get("hooks", []))
        g["hooks"] = kept
    groups = [g for g in groups if g.get("hooks")]
    if groups:
        hooks["SessionEnd"] = groups
    else:
        hooks.pop("SessionEnd", None)
    if hooks:
        data["hooks"] = hooks
    else:
        data.pop("hooks", None)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return removed


SCHEDULES = {"daily": "50 17 * * *", "hourly": "7 * * * *"}


def cron_line(root: Path, every: str = "daily") -> str:
    q = shlex.quote
    return (f"{SCHEDULES[every]} cd {q(str(root))} && PATH={q(os.environ.get('PATH', ''))} "
            f"{q(_cu())} learn --quiet >> {q(str(root / '.context/learn.log'))} 2>&1  # {TAG}: {root}")


def _crontab() -> list[str]:
    out = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    return out.stdout.splitlines() if out.returncode == 0 else []


def _write_crontab(lines: list[str]) -> None:
    subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", text=True, check=True)


def install_cron(root: Path, every: str = "daily") -> str:
    line = cron_line(root, every)
    lines = [l for l in _crontab() if f"# {TAG}: {root}" not in l]
    _write_crontab(lines + [line])
    return line


def remove_cron(root: Path) -> bool:
    lines = _crontab()
    kept = [l for l in lines if f"# {TAG}: {root}" not in l]
    if len(kept) != len(lines):
        _write_crontab(kept)
        return True
    return False
