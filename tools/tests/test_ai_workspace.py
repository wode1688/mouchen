"""Exercise real Git isolation using disposable synthetic repositories."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "ai_workspace.py"


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ai-workspace-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / "example-repo"
        self.repo.mkdir()
        self.environment = os.environ.copy()
        self.environment["GIT_CONFIG_NOSYSTEM"] = "1"
        self.environment["GIT_CONFIG_GLOBAL"] = os.devnull
        self.environment["GIT_TERMINAL_PROMPT"] = "0"
        for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
            self.environment.pop(key, None)
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Example Developer")
        self.git("config", "user.email", "developer@example.invalid")
        (self.repo / "tools").mkdir()
        shutil.copy2(SCRIPT, self.repo / "tools" / SCRIPT.name)
        (self.repo / "AGENTS.md").write_text("Synthetic project instructions.\n", encoding="utf-8")
        (self.repo / "sample.txt").write_text("original\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-m", "Synthetic starting point")
        self.head = self.git("rev-parse", "HEAD").stdout.strip()

    def git(self, *args, repo=None):
        return subprocess.run(
            ["git", "-C", str(repo or self.repo), *args],
            check=True, capture_output=True, text=True,
            encoding="utf-8", env=self.environment,
        )

    def create(self, tool="codex", task="task-one", base="HEAD", offline=True):
        command = [sys.executable, str(self.repo / "tools" / SCRIPT.name),
                   "--tool", tool, "--task", task, "--base", base]
        if offline:
            command.append("--offline")
        return subprocess.run(command, cwd=self.repo, env=self.environment,
                              capture_output=True, text=True, encoding="utf-8")

    def target(self, tool="codex", task="task-one"):
        return self.root / "example-repo-worktrees" / f"{tool}-{task}"

    def test_parallel_tasks_preserve_dirty_source_and_isolate_writes(self):
        (self.repo / "sample.txt").write_text("unfinished local work", encoding="utf-8")
        (self.repo / "untracked.txt").write_text("keep", encoding="utf-8")
        before = self.git("status", "--porcelain").stdout
        for tool in ("codex", "claude", "hermes"):
            result = self.create(tool)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.git("rev-parse", "HEAD", repo=self.target(tool)).stdout.strip(), self.head)
        (self.target() / "sample.txt").write_text("codex change", encoding="utf-8")
        self.assertEqual((self.target("claude") / "sample.txt").read_text(), "original\n")
        self.assertEqual((self.target("hermes") / "sample.txt").read_text(), "original\n")
        self.assertEqual(self.git("status", "--porcelain").stdout, before)
        self.assertEqual(self.git("branch", "--show-current").stdout.strip(), "main")
        record = self.target() / "docs/handoffs/task-one-codex.md"
        self.assertIn(self.head, record.read_text())

    def test_existing_workspace_is_preserved(self):
        self.assertEqual(self.create().returncode, 0)
        marker = self.target() / "unfinished.txt"
        marker.write_text("keep this task", encoding="utf-8")
        self.assertNotEqual(self.create().returncode, 0)
        self.assertEqual(marker.read_text(), "keep this task")

    def test_traversal_and_invalid_names_create_nothing(self):
        for name in ("../escape", "task/one", "a;echo", "..", "x" * 65):
            self.assertNotEqual(self.create(task=name).returncode, 0)
        self.assertFalse((self.root / "example-repo-worktrees").exists())

    def test_unknown_base_creates_no_branch_or_directory(self):
        self.assertNotEqual(self.create(base="missing-reference").returncode, 0)
        self.assertFalse((self.root / "example-repo-worktrees").exists())
        self.assertEqual(self.git("branch", "--list", "ai/*").stdout.strip(), "")

    def test_online_fetch_prevents_stale_remote_task_reuse(self):
        remote = self.root / "remote.git"
        self.git("init", "--bare", str(remote))
        self.git("remote", "add", "origin", str(remote))
        self.git("push", "origin", "main")
        self.assertEqual(self.create(offline=False, base="origin/main").returncode, 0)
        # A second task name has already been published by another clone.
        self.git("push", "origin", "main:refs/heads/ai/claude/already-taken")
        self.git("update-ref", "-d", "refs/remotes/origin/ai/claude/already-taken")
        result = self.create(tool="claude", task="already-taken", offline=False, base="origin/main")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exists on the remote", result.stderr)
        self.assertFalse(self.target("claude", "already-taken").exists())

    def test_fetch_failure_does_not_start_from_stale_data(self):
        result = self.create(offline=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "example-repo-worktrees").exists())


if __name__ == "__main__":
    unittest.main()
