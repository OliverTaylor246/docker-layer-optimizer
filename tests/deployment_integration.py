#!/usr/bin/env python3
"""Exercise the installed deployment profiler without requiring Docker or a device."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def execute(command: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode != 0:
        raise AssertionError(
            f"command failed with {result.returncode}: {command!r}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def profile(root: Path, environment: dict[str, str], secret: str) -> dict:
    program = (
        "import time; "
        f"print('BUILD {secret}', flush=True); time.sleep(0.01); "
        f"print('TRANSFER {secret}', flush=True); time.sleep(0.01); "
        f"print('READY {secret}', flush=True); time.sleep(0.03)"
    )
    result = execute([
        "dlo", "deploy", "--root", str(root), "--adapter", "generic", "--target", "integration",
        "--phase-marker", "build=^BUILD", "--phase-marker", "transfer=^TRANSFER",
        "--phase-marker", "readiness=^READY", "--json", "--", sys.executable, "-c", program,
    ], cwd=root, env=environment)
    return json.loads(result.stdout)


def main() -> int:
    if not shutil.which("dlo"):
        raise SystemExit("an installed dlo command is required")
    with tempfile.TemporaryDirectory(prefix="dlo-deployment-integration-") as directory:
        workspace = Path(directory)
        root, cache = workspace / "project", workspace / "state"
        root.mkdir()
        (root / "Dockerfile").write_text("FROM scratch\nCOPY app.py /app.py\n", encoding="utf-8")
        (root / "app.py").write_text("print('one')\n", encoding="utf-8")
        environment = dict(os.environ, DLO_CACHE_DIR=str(cache))
        secret = "PRIVATE_DEPLOYMENT_OUTPUT_42"

        first = profile(root, environment, secret)
        assert first["status"] == "success"
        assert first["deployment"]["adapter"] == "generic"
        assert first["deployment"]["phases"]["build"]["observed"] is True
        assert first["deployment"]["phases"]["transfer"]["observed"] is True
        assert first["deployment"]["phases"]["readiness"]["observed"] is True

        (root / "app.py").write_text("print('two')\n", encoding="utf-8")
        second = profile(root, environment, secret)
        assert second["changed_paths"] == ["app.py"]

        history = execute(["dlo", "history", "--root", str(root), "--json"], cwd=root, env=environment)
        assert len(json.loads(history.stdout)) == 2
        report = execute(["dlo", "analyze", "--root", str(root), "--json"], cwd=root, env=environment)
        assert json.loads(report.stdout)["evidence"]["measured_deployments"] == 2

        persisted = "\n".join(
            path.read_text(encoding="utf-8", errors="replace") for path in cache.rglob("*") if path.is_file()
        )
        assert secret not in persisted
        assert "import time;" not in persisted
        print(json.dumps({"status": "ok", "deployments": 2, "changed_paths": second["changed_paths"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
