from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
GUARD_PATH = ROOT / "deploy" / "scripts" / "runtime_image_guard.py"
SPEC = importlib.util.spec_from_file_location("runtime_image_guard", GUARD_PATH)
assert SPEC is not None and SPEC.loader is not None
GUARD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GUARD)


OLD_ID = "sha256:" + "1" * 64
OTHER_ID = "sha256:" + "2" * 64
RELEASE_ID = "20260812T123456Z-1234"


class FakeDocker:
    def __init__(self, references: dict[str, str]) -> None:
        self.references = references
        self.commands: list[list[str]] = []

    def __call__(
        self, arguments: list[str], *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        command = list(arguments)
        self.commands.append(command)
        if command[1:4] == ["image", "inspect", "--format"]:
            reference = command[-1]
            image_id = self.references.get(reference)
            return subprocess.CompletedProcess(
                command,
                0 if image_id else 1,
                f"{image_id}\n" if image_id else "",
                "" if image_id else "not found",
            )
        if command[1:3] == ["image", "tag"]:
            source, destination = command[-2:]
            self.references[destination] = self.references[source]
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[1] == "run":
            return subprocess.CompletedProcess(command, 0, "", "")
        raise AssertionError(f"unexpected fake Docker command: {command}")


def test_protect_pins_old_id_before_mutable_build_tag_can_move(monkeypatch) -> None:
    fake = FakeDocker({OLD_ID: OLD_ID, "mouchen-backend:private-alpha": OLD_ID})
    monkeypatch.setattr(GUARD, "_run", fake)

    tag = GUARD.protect(OLD_ID, RELEASE_ID)
    fake.references["mouchen-backend:private-alpha"] = OTHER_ID

    assert tag == f"mouchen-release-guard:previous-{RELEASE_ID}-{'1' * 64}"
    assert GUARD.verify(OLD_ID, RELEASE_ID) == tag
    assert fake.references[tag] == OLD_ID
    assert ["docker", "image", "tag", OLD_ID, tag] in fake.commands


def test_protect_refuses_to_repoint_a_colliding_release_tag(monkeypatch) -> None:
    tag = GUARD.protection_tag(OLD_ID, RELEASE_ID)
    fake = FakeDocker({OLD_ID: OLD_ID, tag: OTHER_ID})
    monkeypatch.setattr(GUARD, "_run", fake)

    with pytest.raises(GUARD.ImageGuardError, match="already points"):
        GUARD.protect(OLD_ID, RELEASE_ID)

    assert fake.references[tag] == OTHER_ID
    assert not any(command[1:3] == ["image", "tag"] for command in fake.commands)


def test_rollback_probe_runs_only_the_verified_release_tag(monkeypatch) -> None:
    tag = GUARD.protection_tag(OLD_ID, RELEASE_ID)
    fake = FakeDocker({tag: OLD_ID})
    monkeypatch.setattr(GUARD, "_run", fake)

    assert GUARD.verify(OLD_ID, RELEASE_ID, probe=True) == tag
    run = next(command for command in fake.commands if command[1] == "run")
    assert tag in run
    assert OLD_ID not in run
    assert "--network" in run and "none" in run
    assert "--read-only" in run
    assert ["--cap-drop", "ALL"] == run[run.index("--cap-drop") : run.index("--cap-drop") + 2]


@pytest.mark.parametrize(
    ("image_id", "release_id"),
    [
        ("latest", RELEASE_ID),
        (OLD_ID, "../escape"),
        (OLD_ID, "x" * 49),
    ],
)
def test_protection_tag_rejects_mutable_or_unsafe_inputs(image_id, release_id) -> None:
    with pytest.raises(GUARD.ImageGuardError):
        GUARD.protection_tag(image_id, release_id)
