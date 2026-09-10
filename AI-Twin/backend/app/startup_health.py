from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from typing import Sequence


_IS_WINDOWS = os.name == "nt"
_MAX_TOTAL_SECONDS = 25.0
_AUTH_PROBE_SECONDS = 3.0
_TERMINATE_SECONDS = 3.0
_DRAIN_SECONDS = 1.0
_SAFE_MODEL = re.compile(r"[A-Za-z0-9._-]+")


@dataclass(frozen=True)
class ClaudeStartupHealth:
    binary: str
    auth: str
    inference: str
    reason: str
    model: str
    elapsed_ms: int

    def summary(self) -> str:
        return (
            f"binary={self.binary}; auth={self.auth}; inference={self.inference}; "
            f"reason={self.reason}; model={self.model}; elapsed_ms={self.elapsed_ms}"
        )


@dataclass(frozen=True)
class _ProcessResult:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False


def _child_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in (
        "MOUCHEN_API_TOKEN",
        "MOUCHEN_API_BEARER_TOKEN",
        "MOUCHEN_DB_PATH",
        "MOUCHEN_SAFETY_SALT",
        "OLLAMA_AUTH_TOKEN",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
    ):
        environment.pop(name, None)
        environment.pop(f"{name}_FILE", None)
    return environment


def _managed_process_options() -> dict[str, object]:
    if _IS_WINDOWS:
        return {
            "creationflags": (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
        }
    return {"start_new_session": True}


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if _IS_WINDOWS:
        try:
            subprocess.run(
                ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_TERMINATE_SECONDS,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            pass
    elif process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass

    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass


def _run_process(
    args: Sequence[str],
    *,
    timeout_seconds: float,
    input_text: str | None = None,
) -> _ProcessResult:
    process = subprocess.Popen(
        list(args),
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_child_environment(),
        cwd=tempfile.gettempdir(),
        **_managed_process_options(),
    )
    try:
        stdout, stderr = process.communicate(input_text, timeout=max(timeout_seconds, 0.01))
        return _ProcessResult(process.returncode, stdout, stderr)
    except subprocess.TimeoutExpired:
        _terminate_process_tree(process)
        try:
            stdout, stderr = process.communicate(timeout=_DRAIN_SECONDS)
        except (OSError, subprocess.SubprocessError):
            stdout, stderr = "", ""
        return _ProcessResult(process.returncode, stdout, stderr, timed_out=True)
    except BaseException:
        _terminate_process_tree(process)
        raise


def _auth_state(result: _ProcessResult) -> str:
    if result.timed_out or result.returncode != 0:
        return "unavailable"
    try:
        payload = json.loads(result.stdout)
    except (TypeError, ValueError):
        return "unknown"
    logged_in = payload.get("loggedIn")
    if logged_in is True:
        return "ready"
    if logged_in is False:
        return "unavailable"
    return "unknown"


def _failure_reason(result: _ProcessResult, auth: str) -> str:
    if result.timed_out:
        return "inference_timeout"
    # Raw CLI output is inspected only for fixed classification. It is never
    # included in the returned snapshot, connection file, or console output.
    diagnostic = f"{result.stdout}\n{result.stderr}".casefold()
    if re.search(r"\b503\b", diagnostic) or any(
        marker in diagnostic
        for marker in (
            "service unavailable",
            "overloaded",
        )
    ):
        return "provider_unavailable"
    if any(
        marker in diagnostic
        for marker in (
            "http 401",
            "status code 401",
            "unauthorized",
            "invalid api key",
            "authentication_error",
            "not logged in",
            "login required",
        )
    ) or auth == "unavailable":
        return "authentication_failed"
    return "inference_failed"


def probe_claude_startup_health(
    *,
    timeout_seconds: float = _MAX_TOTAL_SECONDS,
    command: str | None = None,
    model: str | None = None,
) -> ClaudeStartupHealth:
    started = time.monotonic()
    timeout_seconds = min(max(float(timeout_seconds), 0.1), _MAX_TOTAL_SECONDS)
    deadline = started + timeout_seconds
    command = command or os.getenv("CLAUDE_CODE_COMMAND", "claude")
    model = model or os.getenv("CLAUDE_CODE_REVIEW_MODEL", "claude-fable-5")

    def snapshot(binary: str, auth: str, inference: str, reason: str) -> ClaudeStartupHealth:
        return ClaudeStartupHealth(
            binary=binary,
            auth=auth,
            inference=inference,
            reason=reason,
            model=model,
            elapsed_ms=max(0, int((time.monotonic() - started) * 1000)),
        )

    if not _SAFE_MODEL.fullmatch(model):
        return snapshot("unknown", "unknown", "skipped", "invalid_model")
    executable = shutil.which(command)
    if not executable:
        return snapshot("missing", "unknown", "skipped", "binary_missing")

    auth_timeout = min(_AUTH_PROBE_SECONDS, max(deadline - time.monotonic(), 0.01))
    try:
        auth_result = _run_process(
            [executable, "auth", "status", "--json"],
            timeout_seconds=auth_timeout,
        )
        auth = _auth_state(auth_result)
    except (OSError, subprocess.SubprocessError):
        auth = "unavailable"

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return snapshot("ready", auth, "timeout", "probe_deadline_exceeded")

    try:
        inference_result = _run_process(
            [
                executable,
                "--print",
                "--output-format",
                "text",
                "--no-session-persistence",
                "--safe-mode",
                "--permission-mode",
                "dontAsk",
                "--tools",
                "",
                "--disable-slash-commands",
                "--model",
                model,
            ],
            timeout_seconds=remaining,
            input_text="Reply with only OK.",
        )
    except (OSError, subprocess.SubprocessError):
        return snapshot("ready", auth, "unavailable", "inference_launch_failed")

    if inference_result.timed_out:
        return snapshot("ready", auth, "timeout", "inference_timeout")
    if inference_result.returncode == 0 and inference_result.stdout.strip():
        return snapshot("ready", "ready" if auth == "unavailable" else auth, "ready", "ok")
    reason = _failure_reason(inference_result, auth)
    if reason == "authentication_failed":
        auth = "unavailable"
    return snapshot("ready", auth, "unavailable", reason)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bounded Claude Code startup inference probe")
    parser.add_argument("--timeout-seconds", type=float, default=_MAX_TOTAL_SECONDS)
    args = parser.parse_args(argv)
    try:
        health = probe_claude_startup_health(timeout_seconds=args.timeout_seconds)
    except Exception:
        model = os.getenv("CLAUDE_CODE_REVIEW_MODEL", "claude-fable-5")
        health = ClaudeStartupHealth(
            binary="unknown",
            auth="unknown",
            inference="unavailable",
            reason="probe_error",
            model=model if _SAFE_MODEL.fullmatch(model) else "invalid",
            elapsed_ms=0,
        )
    print(json.dumps(asdict(health), ensure_ascii=True, separators=(",", ":")))
    # Health degradation is informational and must never block backend startup.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
