"""Create one isolated Git worktree for one development task; no AI API calls."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import subprocess
import sys


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    environment = os.environ.copy()
    environment["GIT_TERMINAL_PROMPT"] = "0"
    result = subprocess.run(
        ["git", "-C", str(repo), *args], env=environment,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120,
    )
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or "Git command failed")
    return result


def slug(value: str) -> str:
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value) or len(value) > 64:
        raise argparse.ArgumentTypeError(
            "Use up to 64 lowercase letters, digits and single separating hyphens."
        )
    return value


def create_workspace(args: argparse.Namespace) -> tuple[Path, str, Path]:
    source = Path(__file__).resolve().parents[1]
    repo = Path(git(source, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    project_relative = source.relative_to(repo)
    branch = f"ai/{args.tool}/{args.task}"
    container = repo.parent / f"{repo.name}-worktrees"
    target = container / f"{args.tool}-{args.task}"
    # Resolve before creating anything; do not traverse a pre-existing symlink.
    if container.is_symlink() or container.resolve() != container:
        raise RuntimeError("Workspace parent must be a regular sibling directory.")
    if target.exists() or target.is_symlink():
        raise RuntimeError("Task directory already exists; resume it or use a new task name.")
    if git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0:
        raise RuntimeError("Task branch already exists; use the handoff workflow to resume it.")
    if not args.offline:
        git(repo, "fetch", "origin")
    if git(repo, "show-ref", "--verify", "--quiet", f"refs/remotes/origin/{branch}", check=False).returncode == 0:
        raise RuntimeError("Task branch exists on the remote; use the handoff workflow to resume it.")
    base = git(repo, "rev-parse", "--verify", "--end-of-options", f"{args.base}^{{commit}}").stdout.strip()
    if git(repo, "cat-file", "-e", f"{base}:AGENTS.md", check=False).returncode:
        raise RuntimeError("Base does not contain shared AGENTS.md; select the collaboration branch with --base.")
    record_name = (project_relative / "docs" / "handoffs" / f"{args.task}-{args.tool}.md").as_posix()
    if git(repo, "cat-file", "-e", f"{base}:{record_name}", check=False).returncode == 0:
        raise RuntimeError("A handoff record for this task already exists in the base commit.")
    container.mkdir(exist_ok=True)
    git(repo, "worktree", "add", "-b", branch, str(target), base)
    record = target / record_name
    # A repository-provided link must not redirect the task record outside its worktree.
    if not record.resolve().is_relative_to(target.resolve()):
        raise RuntimeError("Handoff directory points outside the new worktree; inspect it before continuing.")
    record.parent.mkdir(parents=True, exist_ok=True)
    created = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with record.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(f"""# Task: {args.task}

- Tool: {args.tool}
- Branch: `{branch}`
- Starting commit: `{base}`
- Created (UTC): {created}
- Status: prepared locally; no remote task claim has been made by this script
- Issue: to be recorded after checking the shared task list

## Goal and acceptance criteria

To be filled from the assigned task.

## Scope, owner and dependencies

Record the agreed file/module scope and current executor. Use project aliases, not private device identifiers.

## Completed work

No task implementation has been completed yet.

## Verification evidence

No task tests have been run yet. Record commands, outcomes and the tested commit.

## Remaining work and next step

Read AGENTS.md, confirm the task assignment, inspect the actual Git state and fill this record.

## Handoff

Record the last completed commit and the next executor before handing over.
Commit this record with the task changes; obtain its own commit from Git history afterward.
Do not include credentials, personal data, private paths, server addresses or raw chat logs.
""")
    return target, branch, record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tool", required=True, type=slug, help="codex, claude, hermes or another tool name")
    parser.add_argument("--task", required=True, type=slug, help="unique task name, e.g. issue-101-sync")
    parser.add_argument("--base", default="origin/main", help="committed starting reference (default: origin/main)")
    parser.add_argument("--offline", action="store_true", help="explicitly skip fetching remote updates")
    args = parser.parse_args()
    try:
        target, branch, record = create_workspace(args)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"Cannot create task workspace: {exc}", file=sys.stderr)
        return 1
    print(f"Workspace: {target}\nBranch: {branch}\nHandoff: {record}")
    print("Open this directory in the chosen tool. No AI tool was started or task claimed remotely.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
