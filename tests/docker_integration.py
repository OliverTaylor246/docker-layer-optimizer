#!/usr/bin/env python3
"""Run real-Docker lifecycle tests against the installed `dlo` command."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import uuid


DOCKERFILE = """# syntax=docker/dockerfile:1
FROM alpine:3.21 AS build
WORKDIR /work
COPY stable.txt .
RUN --mount=type=cache,target=/cache cp stable.txt /cache/stable && cp stable.txt built.txt
COPY app.txt .
RUN cp app.txt built-app.txt
FROM alpine:3.21
COPY --from=build /work/built.txt /stable.txt
COPY --from=build /work/built-app.txt /app.txt
"""


def execute(command: list[str], *, cwd: Path, env: dict[str, str], expect: int = 0) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode != expect:
        raise AssertionError(
            f"expected exit {expect}, got {result.returncode}: {' '.join(command)}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def measured(root: Path, env: dict[str, str], tag: str, *extra: str, expect: int = 0) -> dict:
    result = execute(
        ["dlo", "build", "--root", str(root), "--tag", tag, "--quiet", "--json", *extra],
        cwd=root, env=env, expect=expect,
    )
    stream = result.stdout if result.stdout.strip() else result.stderr
    try:
        return json.loads(stream)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"dlo did not emit JSON:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}") from exc


def assert_private(cache: Path, forbidden: list[str]) -> None:
    persisted = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in cache.rglob("*") if path.is_file()
    )
    for value in forbidden:
        if value in persisted:
            raise AssertionError(f"private value was persisted: {value!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", default=os.environ.get("DLO_TEST_REGISTRY"), help="optional registry host:port")
    args = parser.parse_args()
    if not shutil.which("docker") or not shutil.which("dlo"):
        raise SystemExit("docker and an installed dlo command are required")

    unique = uuid.uuid4().hex[:10]
    local_tag = f"dlo-integration:{unique}"
    registry_tag = f"{args.registry}/dlo/integration:{unique}" if args.registry else None
    created_tags = [local_tag]
    with tempfile.TemporaryDirectory(prefix="dlo-integration-") as directory:
        workspace = Path(directory)
        root, cache = workspace / "project", workspace / "state"
        root.mkdir()
        (root / ".dockerignore").write_text("ignored/\nsecret*\n", encoding="utf-8")
        (root / "stable.txt").write_text(f"dependency-v1-{unique}\n", encoding="utf-8")
        (root / "app.txt").write_text(f"source-v1-{unique}\n", encoding="utf-8")
        (root / "Dockerfile").write_text(DOCKERFILE, encoding="utf-8")
        environment = dict(os.environ, DLO_CACHE_DIR=str(cache))

        try:
            baseline = measured(root, environment, local_tag)
            no_op = measured(root, environment, local_tag)
            assert baseline["steps"]["progress_format"] == "rawjson"
            assert no_op["steps"]["rebuilt"] == 0
            assert no_op["steps"]["cached"] >= 4
            assert no_op["image"]["unmatched_diff_ids"] == 0
            assert no_op["image"]["has_baseline"] is True

            (root / "app.txt").write_text(f"source-v2-{unique}\n", encoding="utf-8")
            source_edit = measured(root, environment, local_tag)
            assert source_edit["steps"]["rebuilt"] > 0
            assert source_edit["steps"]["cached"] > 0
            assert source_edit["changed_paths"] == ["app.txt"]

            (root / "stable.txt").write_text(f"dependency-v2-{unique}\n", encoding="utf-8")
            dependency_edit = measured(root, environment, local_tag)
            assert dependency_edit["steps"]["rebuilt"] > source_edit["steps"]["rebuilt"]

            original = (root / "Dockerfile").read_text(encoding="utf-8")
            (root / "Dockerfile").write_text(original.replace("alpine:3.21", "alpine:3.22"), encoding="utf-8")
            base_edit = measured(root, environment, local_tag)
            assert base_edit["image"]["unmatched_diff_ids"] > 0

            (root / "Dockerfile").write_text(original + "RUN false\n", encoding="utf-8")
            failure = measured(root, environment, local_tag, expect=1)
            assert failure["status"] == "failure"
            assert failure["steps"]["failed"] >= 1
            (root / "Dockerfile").write_text(original, encoding="utf-8")
            recovery = measured(root, environment, local_tag)
            assert recovery["status"] == "success"
            assert recovery["image"]["has_baseline"] is True

            secret_file = workspace / "outside-secret.txt"
            secret_value = f"integration-secret-{unique}"
            secret_file.write_text(secret_value, encoding="utf-8")
            (root / "Dockerfile").write_text(
                "# syntax=docker/dockerfile:1\nFROM alpine:3.21\n"
                "RUN --mount=type=secret,id=token test -s /run/secrets/token\nCOPY app.txt /app.txt\n",
                encoding="utf-8",
            )
            secret_observation = measured(root, environment, local_tag, "--secret", f"id=token,src={secret_file}")
            assert secret_observation["status"] == "success"
            assert_private(cache, [secret_value, str(secret_file)])

            (root / "Dockerfile").write_text(original, encoding="utf-8")
            commands = [
                ["dlo", "build", "--root", str(root), "--tag", local_tag, "--quiet", "--json"]
                for _ in range(2)
            ]
            processes = [subprocess.Popen(command, cwd=root, env=environment, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for command in commands]
            concurrent = [process.communicate(timeout=180) + (process.returncode,) for process in processes]
            if any(code != 0 for _, _, code in concurrent):
                raise AssertionError(f"concurrent builds failed: {concurrent}")
            history = execute(["dlo", "history", "--root", str(root), "--limit", "100", "--json"], cwd=root, env=environment)
            assert isinstance(json.loads(history.stdout), list)

            registry_summary = None
            if registry_tag:
                created_tags.append(registry_tag)
                first_push = measured(root, environment, registry_tag, "--push")
                second_push = measured(root, environment, registry_tag, "--push")
                assert first_push["registry"]["has_baseline"] is False
                assert second_push["registry"]["has_baseline"] is True
                assert second_push["registry"]["unmatched_blobs"] == 0
                assert second_push["registry"]["unmatched_compressed_bytes"] == 0
                (root / "app.txt").write_text(f"registry-source-{time.time_ns()}\n", encoding="utf-8")
                changed_push = measured(root, environment, registry_tag, "--push")
                assert changed_push["registry"]["unmatched_blobs"] > 0
                assert changed_push["registry"]["matching_blobs"] > 0
                registry_summary = {
                    "baseline_blobs": first_push["registry"]["total_layers"],
                    "no_op_unmatched_bytes": second_push["registry"]["unmatched_compressed_bytes"],
                    "source_edit_unmatched_bytes": changed_push["registry"]["unmatched_compressed_bytes"],
                }

            print(json.dumps({
                "status": "ok",
                "progress_format": baseline["steps"]["progress_format"],
                "no_op_rebuilt_steps": no_op["steps"]["rebuilt"],
                "source_edit_rebuilt_steps": source_edit["steps"]["rebuilt"],
                "dependency_edit_rebuilt_steps": dependency_edit["steps"]["rebuilt"],
                "registry": registry_summary,
            }, indent=2, sort_keys=True))
        finally:
            for tag in created_tags:
                subprocess.run(["docker", "image", "rm", tag], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
