from __future__ import annotations

import importlib.util
import inspect
import json
import sys
from pathlib import Path

import pytest

from mouchen_desktop import launcher
from mouchen_desktop.launcher import run_packaged_self_test, run_packaged_ui_smoke


DESKTOP_ROOT = Path(__file__).resolve().parents[1]
AUDIT_PATH = DESKTOP_ROOT / "packaging" / "audit_sensitive.py"
AUDIT_SPEC = importlib.util.spec_from_file_location("mouchen_sensitive_audit", AUDIT_PATH)
assert AUDIT_SPEC is not None and AUDIT_SPEC.loader is not None
AUDIT = importlib.util.module_from_spec(AUDIT_SPEC)
sys.modules[AUDIT_SPEC.name] = AUDIT
AUDIT_SPEC.loader.exec_module(AUDIT)


def test_self_test_proves_first_run_account_gate_without_credentials(tmp_path):
    report = tmp_path / "self-test.json"

    assert run_packaged_self_test(["--self-test-report", str(report)]) == 0
    text = report.read_text(encoding="utf-8")

    assert '"embedded_credentials": false' in text
    assert '"service_address"' in text
    assert '"login"' in text
    assert '"register"' in text


def test_non_graphical_self_test_does_not_import_the_ui_stack():
    source = inspect.getsource(run_packaged_self_test)

    assert "find_spec" not in source
    assert "from .ui import" not in source


def test_windowed_probe_failure_writes_a_report_instead_of_opening_a_dialog(
    monkeypatch,
    tmp_path,
):
    report = tmp_path / "probe-error.json"

    def fail_probe():
        raise RuntimeError("synthetic-secret-value-must-not-be-recorded")

    monkeypatch.setattr(launcher, "run_packaged_self_test", fail_probe)
    monkeypatch.setattr(
        sys,
        "argv",
        ["Mouchen.exe", "--self-test", "--self-test-report", str(report)],
    )

    assert launcher.main() == 90
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["probe"] == "self-test"
    assert payload["error_type"] == "RuntimeError"
    assert payload["success"] is False
    assert "error_message" not in payload
    assert "synthetic-secret-value" not in report.read_text(encoding="utf-8")


def test_packaging_inputs_do_not_contain_secret_material_or_runtime_data():
    package_inputs = [
        DESKTOP_ROOT / "MouchenDesktop.pyw",
        *sorted((DESKTOP_ROOT / "mouchen_desktop").glob("*.py")),
    ]
    secret_markers = (
        "-----BEGIN " + "PRIVATE KEY-----",
        "-----BEGIN " + "OPENSSH PRIVATE KEY-----",
        "sk-" + "proj-",
        "sk-" + "ant-",
    )

    assert package_inputs
    assert all(path.suffix in {".py", ".pyw"} for path in package_inputs)
    assert all(
        marker not in path.read_text(encoding="utf-8")
        for path in package_inputs
        for marker in secret_markers
    )
    assert not any(path.name in {"settings.json", ".env"} for path in package_inputs)
    assert not any(path.suffix in {".db", ".pem", ".key"} for path in package_inputs)
    assert AUDIT.scan_source_tree(DESKTOP_ROOT) == []


def test_packaged_probes_have_a_bounded_wait():
    build_script = (DESKTOP_ROOT / "build_windows.ps1").read_text(encoding="utf-8")

    assert "WaitForExit(60000)" in build_script
    assert "timed out after 60 seconds" in build_script
    assert "Start-Process" in build_script
    assert "-Wait -PassThru" not in build_script
    assert "Write-PackagedProbeDiagnostic" in build_script
    assert "error_type=$errorType" in build_script
    assert "Write-Host $diagnostic" not in build_script
    assert 'SetEnvironmentVariable("MOUCHEN_BACKEND_URL", "http://127.0.0.1:8787"' in build_script


def test_sensitive_audit_detects_urls_keys_tokens_jwts_and_credential_constants(tmp_path):
    source = tmp_path / "sample.py"
    token = "sk-" + "synthetic_test_value"
    jwt = "eyJ" + "abcdefghijk" + "." + "abcdefghijkl" + "." + "abcdefghijkl"
    private_key = "-----BEGIN " + "PRIVATE KEY-----\\nsynthetic\\n-----END " + "PRIVATE KEY-----"
    source.write_text(
        '\n'.join(
            (
                'SERVICE_URL = "https://example.invalid/v1"',
                'SERVICE_PASSWORD = "not-a-real-password"',
                f'TEST_TOKEN = "{token}"',
                f'TEST_JWT = "{jwt}"',
                f'TEST_KEY = "{private_key}"',
            )
        ),
        encoding="utf-8",
    )

    rules = {finding.rule for finding in AUDIT.scan_source_file(source)}

    assert {
        "non-loopback-url",
        "credential-constant",
        "api-token",
        "jwt",
        "private-key",
    }.issubset(rules)


def test_sensitive_audit_does_not_flag_authorization_logic_or_loopback(tmp_path):
    source = tmp_path / "safe.py"
    source.write_text(
        '\n'.join(
            (
                'LOCAL_URL = "http://127.0.0.1:8787"',
                'AUTHORIZATION_HEADER = "Authorization"',
                'bearer_token = ""',
                'headers[AUTHORIZATION_HEADER] = f"Bearer {bearer_token}"',
            )
        ),
        encoding="utf-8",
    )

    assert AUDIT.scan_source_file(source) == []


def test_package_audit_finds_environment_secret_without_echoing_value(tmp_path):
    secret = "not-a-real-environment-secret-12345"
    binary = tmp_path / "payload.bin"
    binary.write_bytes(b"prefix\x00" + secret.encode("utf-8") + b"\x00suffix")

    findings = AUDIT.scan_package_tree(
        tmp_path,
        environment={"MOUCHEN_TEST_API_KEY": secret},
    )
    messages = "\n".join(finding.safe_message() for finding in findings)

    assert len(findings) == 1
    assert "MOUCHEN_TEST_API_KEY" in messages
    assert secret not in messages


@pytest.mark.parametrize(
    ("password_mask", "confirmation_mask", "expected_exit"),
    (("●", "●", 0), ("●", "", 4)),
)
def test_ui_smoke_reads_real_password_widget_masks(
    monkeypatch,
    tmp_path,
    password_mask,
    confirmation_mask,
    expected_exit,
):
    class FakeValue:
        def __init__(self, value):
            self.value = value

        def get(self):
            return self.value

    class FakeEntry:
        def __init__(self, mask):
            self.mask = mask

        def cget(self, option):
            assert option == "show"
            return self.mask

    class FakeButton:
        def __init__(self, text):
            self.text = text

        def cget(self, option):
            assert option == "text"
            return self.text

    class FakeWindow:
        def winfo_viewable(self):
            return True

        def title(self):
            return "登录AI替身"

        def destroy(self):
            return None

    class FakeRoot:
        def geometry(self, _value):
            return None

        def update_idletasks(self):
            return None

        def update(self):
            return None

        def destroy(self):
            return None

    class FakeAccountDialog:
        def __init__(self, _root, _agent):
            self.window = FakeWindow()
            self.backend = FakeValue("http://127.0.0.1:8787")
            self.login_button = FakeButton("登录")
            self.register_button = FakeButton("创建账号")
            self.password_entry = FakeEntry(password_mask)
            self.password_confirmation_entry = FakeEntry(confirmation_mask)

    monkeypatch.setattr("tkinter.Tk", FakeRoot)
    monkeypatch.setattr("mouchen_desktop.ui.AccountDialog", FakeAccountDialog)
    report = tmp_path / "ui-smoke.json"

    exit_code = run_packaged_ui_smoke(["--self-test-report", str(report)])
    payload = json.loads(report.read_text(encoding="utf-8"))

    assert exit_code == expected_exit
    assert payload["password_masked"] is (expected_exit == 0)
