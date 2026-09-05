from __future__ import annotations

import argparse
import ast
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


_URL_PATTERN = re.compile(r"https?://[^\s\"'<>，。；、]+", re.IGNORECASE)
_PRIVATE_KEY_PATTERN = re.compile(
    r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
)
_JWT_PATTERN = re.compile(
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
)
_TOKEN_PATTERN = re.compile(
    r"\b(?:sk|pk|pat|ghp|github_pat|xox[baprs])[-_][A-Za-z0-9._-]{16,}\b",
    re.IGNORECASE,
)
_CREDENTIAL_NAME_PATTERN = re.compile(
    r"(?:^|_)(?:api_?key|access_?key|private_?key|token|secret|password|passcode|credentials?)(?:_|$)",
    re.IGNORECASE,
)
_SENSITIVE_ENVIRONMENT_NAME_PATTERN = re.compile(
    r"(?:^|_)(?:api_?key|access_?key|private_?key|token|secret|password|passcode|credentials?|auth_?token)(?:_|$)",
    re.IGNORECASE,
)
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


@dataclass(frozen=True, slots=True)
class Finding:
    path: str
    rule: str
    detail: str

    def safe_message(self) -> str:
        return f"{self.path}: {self.rule}: {self.detail}"


def _assigned_names(node: ast.Assign | ast.AnnAssign) -> list[str]:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    names: list[str] = []
    for target in targets:
        if isinstance(target, ast.Name):
            names.append(target.id)
        elif isinstance(target, ast.Attribute):
            names.append(target.attr)
    return names


def _literal_string(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def scan_source_file(path: Path, *, display_root: Path | None = None) -> list[Finding]:
    text = path.read_text(encoding="utf-8")
    display_path = str(path.relative_to(display_root)) if display_root else str(path)
    findings: list[Finding] = []

    if _PRIVATE_KEY_PATTERN.search(text):
        findings.append(Finding(display_path, "private-key", "private key material found"))
    if _JWT_PATTERN.search(text):
        findings.append(Finding(display_path, "jwt", "JWT-like literal found"))
    if _TOKEN_PATTERN.search(text):
        findings.append(Finding(display_path, "api-token", "API token-like literal found"))

    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as exc:
        findings.append(Finding(display_path, "python-syntax", f"line {exc.lineno or 0}"))
        return findings

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for match in _URL_PATTERN.finditer(node.value):
                parsed = urlparse(match.group(0))
                host = (parsed.hostname or "").casefold()
                if host and host not in _LOOPBACK_HOSTS:
                    findings.append(
                        Finding(
                            display_path,
                            "non-loopback-url",
                            f"line {getattr(node, 'lineno', 0)} host {host}",
                        )
                    )
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            literal = _literal_string(node.value)
            if not literal or len(literal.strip()) < 8:
                continue
            for name in _assigned_names(node):
                if _CREDENTIAL_NAME_PATTERN.search(name):
                    findings.append(
                        Finding(
                            display_path,
                            "credential-constant",
                            f"line {getattr(node, 'lineno', 0)} variable {name}",
                        )
                    )
    return findings


def scan_source_tree(desktop_root: Path) -> list[Finding]:
    inputs = [desktop_root / "MouchenDesktop.pyw"]
    inputs.extend(sorted((desktop_root / "mouchen_desktop").rglob("*.py")))
    findings: list[Finding] = []
    for path in inputs:
        if not path.is_file():
            findings.append(Finding(str(path), "missing-input", "packaging input is missing"))
            continue
        findings.extend(scan_source_file(path, display_root=desktop_root))
    return findings


def sensitive_environment_values(
    environment: dict[str, str] | os._Environ[str] | None = None,
) -> dict[str, bytes]:
    source = os.environ if environment is None else environment
    values: dict[str, bytes] = {}
    for name, value in source.items():
        clean = str(value).strip()
        if not _SENSITIVE_ENVIRONMENT_NAME_PATTERN.search(str(name)):
            continue
        if len(clean) < 8 or clean.casefold() in {"true", "false", "none", "null"}:
            continue
        values[str(name)] = clean.encode("utf-8")
    return values


def scan_package_tree(
    package_root: Path,
    *,
    environment: dict[str, str] | os._Environ[str] | None = None,
) -> list[Finding]:
    candidates = sensitive_environment_values(environment)
    if not package_root.is_dir():
        return [Finding(str(package_root), "missing-package", "package directory is missing")]
    if not candidates:
        return []

    findings: list[Finding] = []
    for path in sorted(item for item in package_root.rglob("*") if item.is_file()):
        data = path.read_bytes()
        for name, value in candidates.items():
            encodings = (value, value.decode("utf-8").encode("utf-16-le"))
            if any(encoded and encoded in data for encoded in encodings):
                findings.append(
                    Finding(
                        str(path.relative_to(package_root)),
                        "environment-secret",
                        f"value from environment variable {name} found",
                    )
                )
    return findings


def _emit_and_exit(findings: list[Finding]) -> int:
    if not findings:
        return 0
    print("Sensitive packaging audit failed:", file=sys.stderr)
    for finding in findings:
        print(f"- {finding.safe_message()}", file=sys.stderr)
    return 2


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    source_parser = subparsers.add_parser("source")
    source_parser.add_argument("desktop_root", type=Path)
    package_parser = subparsers.add_parser("package")
    package_parser.add_argument("package_root", type=Path)
    args = parser.parse_args(arguments)

    if args.command == "source":
        return _emit_and_exit(scan_source_tree(args.desktop_root.resolve()))
    return _emit_and_exit(scan_package_tree(args.package_root.resolve()))


if __name__ == "__main__":
    raise SystemExit(main())
