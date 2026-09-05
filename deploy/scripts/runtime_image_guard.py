#!/usr/bin/env python3
"""Pin and verify release-unique local Docker rollback image references.

Docker containers retain their configured image id, but a Compose build may
replace the only mutable tag for that id.  Some Docker/BuildKit combinations
then make the old id unavailable to ``docker run`` even while the old
container is still running.  This guard creates a deterministic tag before
the build and never repoints or deletes it.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from collections.abc import Sequence


IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
RELEASE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,47}\Z")
PROTECTION_REPOSITORY = "mouchen-release-guard"


class ImageGuardError(RuntimeError):
    """The requested immutable rollback image cannot be proven available."""


def protection_tag(image_id: str, release_id: str) -> str:
    if IMAGE_ID.fullmatch(image_id) is None:
        raise ImageGuardError("previous image is not an immutable local image id")
    if RELEASE_ID.fullmatch(release_id) is None:
        raise ImageGuardError("release id is unsafe or too long for a protection tag")
    digest = image_id.removeprefix("sha256:")
    return f"{PROTECTION_REPOSITORY}:previous-{release_id}-{digest}"


def _run(arguments: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(arguments),
            check=check,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise ImageGuardError("Docker could not preserve the previous runtime image") from error


def _inspect(reference: str, *, required: bool) -> str | None:
    result = _run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", reference],
        check=False,
    )
    if result.returncode != 0:
        if required:
            raise ImageGuardError("protected previous runtime image is unavailable")
        return None
    image_id = result.stdout.strip()
    if IMAGE_ID.fullmatch(image_id) is None:
        raise ImageGuardError("Docker returned a non-immutable image id")
    return image_id


def protect(image_id: str, release_id: str) -> str:
    tag = protection_tag(image_id, release_id)
    if _inspect(image_id, required=True) != image_id:
        raise ImageGuardError("previous image id does not resolve to itself")
    existing = _inspect(tag, required=False)
    if existing is not None and existing != image_id:
        raise ImageGuardError("release protection tag already points to another image")
    if existing is None:
        _run(["docker", "image", "tag", image_id, tag])
    if _inspect(tag, required=True) != image_id:
        raise ImageGuardError("release protection tag did not retain the previous image")
    return tag


def verify(image_id: str, release_id: str, *, probe: bool = False) -> str:
    tag = protection_tag(image_id, release_id)
    if _inspect(tag, required=True) != image_id:
        raise ImageGuardError("release protection tag points to another image")
    if probe:
        # This is deliberately stronger than image inspect: rollback must know
        # the protected image can create and run a process before database
        # restoration starts.
        _run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--pids-limit",
                "32",
                "--memory",
                "128m",
                "--cpus",
                "0.5",
                "--user",
                "10001:10001",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,nodev,size=8m",
                "--entrypoint",
                "python",
                tag,
                "-c",
                "raise SystemExit(0)",
            ]
        )
    return tag


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("protect", "verify"))
    parser.add_argument("--image", required=True)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--probe", action="store_true")
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    options = _parser().parse_args(arguments)
    try:
        if options.action == "protect":
            if options.probe:
                raise ImageGuardError("--probe is valid only with verify")
            tag = protect(options.image, options.release_id)
        else:
            tag = verify(options.image, options.release_id, probe=options.probe)
    except ImageGuardError as error:
        print(str(error), file=__import__("sys").stderr)
        return 1
    print(tag)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
