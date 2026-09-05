import json
import subprocess

from app import startup_health


class _Process:
    _next_pid = 7000

    def __init__(self, returncode, stdout, stderr, calls, args, kwargs):
        type(self)._next_pid += 1
        self.pid = type(self)._next_pid
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.calls = calls
        self.args = args
        self.kwargs = kwargs

    def communicate(self, input_text=None, timeout=None):
        self.calls.append((self.args, self.kwargs, input_text, timeout))
        return self.stdout, self.stderr

    def poll(self):
        return self.returncode


def test_missing_binary_is_sanitized_and_skips_inference(monkeypatch):
    monkeypatch.setattr(startup_health.shutil, "which", lambda _command: None)
    monkeypatch.setattr(
        startup_health.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not launch")),
    )

    health = startup_health.probe_claude_startup_health()

    assert health.binary == "missing"
    assert health.auth == "unknown"
    assert health.inference == "skipped"
    assert health.reason == "binary_missing"


def test_probe_runs_auth_and_real_minimal_inference_without_mouchen_secrets(monkeypatch):
    calls = []
    results = [
        (0, json.dumps({"loggedIn": True}), ""),
        (0, "OK\n", ""),
    ]

    def popen(args, **kwargs):
        returncode, stdout, stderr = results.pop(0)
        return _Process(returncode, stdout, stderr, calls, args, kwargs)

    monkeypatch.setenv("MOUCHEN_API_TOKEN", "private-backend-token")
    monkeypatch.setenv("MOUCHEN_API_BEARER_TOKEN", "private-bearer-token")
    monkeypatch.setenv("OPENAI_API_KEY_FILE", "/run/secrets/openai-key")
    monkeypatch.setattr(startup_health.shutil, "which", lambda _command: "claude.exe")
    monkeypatch.setattr(startup_health.subprocess, "Popen", popen)

    health = startup_health.probe_claude_startup_health(timeout_seconds=10)

    assert health.binary == health.auth == health.inference == "ready"
    assert health.reason == "ok"
    auth_args, auth_kwargs, auth_input, _ = calls[0]
    assert auth_args == ["claude.exe", "auth", "status", "--json"]
    assert auth_input is None
    inference_args, inference_kwargs, inference_input, _ = calls[1]
    assert "--no-session-persistence" in inference_args
    assert "--safe-mode" in inference_args
    assert inference_args[inference_args.index("--tools") + 1] == ""
    assert inference_args[inference_args.index("--model") + 1] == "claude-fable-5"
    assert inference_input == "Reply with only OK."
    for kwargs in (auth_kwargs, inference_kwargs):
        assert "MOUCHEN_API_TOKEN" not in kwargs["env"]
        assert "MOUCHEN_API_BEARER_TOKEN" not in kwargs["env"]
        assert "OPENAI_API_KEY_FILE" not in kwargs["env"]


def test_provider_503_is_nonfatal_and_raw_output_is_not_returned(monkeypatch):
    calls = []
    results = [
        (0, json.dumps({"loggedIn": True}), ""),
        (1, "", "API Error: 503 secret-provider-detail"),
    ]

    def popen(args, **kwargs):
        returncode, stdout, stderr = results.pop(0)
        return _Process(returncode, stdout, stderr, calls, args, kwargs)

    monkeypatch.setattr(startup_health.shutil, "which", lambda _command: "claude.exe")
    monkeypatch.setattr(startup_health.subprocess, "Popen", popen)

    health = startup_health.probe_claude_startup_health(timeout_seconds=10)
    serialized = json.dumps(health.__dict__)

    assert health.binary == "ready"
    assert health.auth == "ready"
    assert health.inference == "unavailable"
    assert health.reason == "provider_unavailable"
    assert "secret-provider-detail" not in serialized
    assert "503" not in health.summary()


def test_authentication_failure_is_distinct_from_provider_failure(monkeypatch):
    calls = []
    results = [
        (0, json.dumps({"loggedIn": False}), ""),
        (1, "", "HTTP 401 invalid API key private-value"),
    ]

    def popen(args, **kwargs):
        returncode, stdout, stderr = results.pop(0)
        return _Process(returncode, stdout, stderr, calls, args, kwargs)

    monkeypatch.setattr(startup_health.shutil, "which", lambda _command: "claude.exe")
    monkeypatch.setattr(startup_health.subprocess, "Popen", popen)

    health = startup_health.probe_claude_startup_health(timeout_seconds=10)

    assert health.auth == "unavailable"
    assert health.inference == "unavailable"
    assert health.reason == "authentication_failed"
    assert "private-value" not in health.summary()


def test_inference_401_overrides_stale_cached_auth_status(monkeypatch):
    calls = []
    results = [
        (0, json.dumps({"loggedIn": True}), ""),
        (1, "", "Unauthorized"),
    ]

    def popen(args, **kwargs):
        returncode, stdout, stderr = results.pop(0)
        return _Process(returncode, stdout, stderr, calls, args, kwargs)

    monkeypatch.setattr(startup_health.shutil, "which", lambda _command: "claude.exe")
    monkeypatch.setattr(startup_health.subprocess, "Popen", popen)

    health = startup_health.probe_claude_startup_health(timeout_seconds=10)

    assert health.auth == "unavailable"
    assert health.inference == "unavailable"
    assert health.reason == "authentication_failed"


def test_timeout_terminates_process_tree(monkeypatch):
    terminated = []

    class TimeoutProcess:
        pid = 8123
        returncode = None
        communications = 0

        def communicate(self, _input_text=None, timeout=None):
            self.communications += 1
            if self.communications == 1:
                raise subprocess.TimeoutExpired("claude", timeout)
            self.returncode = 1
            return "sensitive stdout", "sensitive stderr"

        def poll(self):
            return self.returncode

    process = TimeoutProcess()
    monkeypatch.setattr(startup_health.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(
        startup_health,
        "_terminate_process_tree",
        lambda candidate: terminated.append(candidate.pid),
    )

    result = startup_health._run_process(["claude.exe"], timeout_seconds=0.01)

    assert result.timed_out is True
    assert terminated == [8123]


def test_windows_tree_termination_uses_descendants_and_force(monkeypatch):
    calls = []

    class Process:
        pid = 8224
        returncode = None

        def poll(self):
            return self.returncode

        def kill(self):
            raise AssertionError("taskkill should terminate the root process")

    process = Process()

    def run(args, **kwargs):
        calls.append((args, kwargs))
        process.returncode = 1
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(startup_health, "_IS_WINDOWS", True)
    monkeypatch.setattr(startup_health.subprocess, "run", run)

    startup_health._terminate_process_tree(process)

    args, kwargs = calls[0]
    assert args == ["taskkill.exe", "/PID", "8224", "/T", "/F"]
    assert kwargs["timeout"] == startup_health._TERMINATE_SECONDS
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL


def test_cli_returns_zero_for_degraded_health(monkeypatch, capsys):
    monkeypatch.setattr(
        startup_health,
        "probe_claude_startup_health",
        lambda **_kwargs: startup_health.ClaudeStartupHealth(
            binary="ready",
            auth="ready",
            inference="unavailable",
            reason="provider_unavailable",
            model="claude-fable-5",
            elapsed_ms=12,
        ),
    )

    assert startup_health.main(["--timeout-seconds", "1"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["inference"] == "unavailable"
    assert payload["reason"] == "provider_unavailable"
