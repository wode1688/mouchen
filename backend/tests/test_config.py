import pytest

from app.config import SecretConfigurationError, secret_value


def test_secret_value_reads_file_and_removes_only_line_endings(tmp_path, monkeypatch):
    secret_file = tmp_path / "secret"
    secret_file.write_text("  punctuation-%  \r\n", encoding="utf-8")
    monkeypatch.delenv("TEST_SECRET", raising=False)
    monkeypatch.setenv("TEST_SECRET_FILE", str(secret_file))

    assert secret_value("TEST_SECRET") == "  punctuation-%  "


def test_direct_secret_takes_precedence_over_file(tmp_path, monkeypatch):
    secret_file = tmp_path / "secret"
    secret_file.write_text("file-value", encoding="utf-8")
    monkeypatch.setenv("TEST_SECRET", " direct-value ")
    monkeypatch.setenv("TEST_SECRET_FILE", str(secret_file))

    assert secret_value("TEST_SECRET") == "direct-value"


def test_missing_secret_file_fails_without_disclosing_path(tmp_path, monkeypatch):
    missing = tmp_path / "private-provider-key"
    monkeypatch.delenv("TEST_SECRET", raising=False)
    monkeypatch.setenv("TEST_SECRET_FILE", str(missing))

    with pytest.raises(SecretConfigurationError) as error:
        secret_value("TEST_SECRET")

    assert str(missing) not in str(error.value)
