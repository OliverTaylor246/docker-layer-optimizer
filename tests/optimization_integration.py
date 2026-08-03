#!/usr/bin/env python3
"""Run the complete agent-first optimization lifecycle against real Docker."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


DOCKERFILE = """FROM alpine:3.21
WORKDIR /app
COPY . .
RUN echo "pip install -r requirements.txt" > /dependency-proof && sleep 1
CMD ["cat", "app.py"]
"""


def execute(command: list[str], root: Path, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command, cwd=root, env=environment, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"command failed ({result.returncode}): {' '.join(command)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def main() -> int:
    if not shutil.which("docker") or not shutil.which("dlo"):
        raise SystemExit("docker and an installed dlo command are required")
    with tempfile.TemporaryDirectory(prefix="dlo-optimize-integration-") as directory:
        workspace = Path(directory)
        root, cache = workspace / "project", workspace / "state"
        root.mkdir()
        (root / "Dockerfile").write_text(DOCKERFILE, encoding="utf-8")
        (root / "requirements.txt").write_text("example==1.0\n", encoding="utf-8")
        (root / "app.py").write_text("print('healthy')\n", encoding="utf-8")
        (root / ".dlo.yml").write_text(
            """version: 1
verification:
  commands:
    - python -m py_compile app.py
benchmark:
  source_path: app.py
  trials: 3
  budget_seconds: 180
  min_relative_improvement: 0.10
  min_absolute_seconds: 0.25
  max_absolute_regression_seconds: 0.75
  payback_deploys: 20
""",
            encoding="utf-8",
        )
        environment = dict(os.environ, DLO_CACHE_DIR=str(cache))
        execute(["git", "init", "-q"], root, environment)
        execute(["git", "config", "user.name", "DLO Integration"], root, environment)
        execute(["git", "config", "user.email", "dlo@example.invalid"], root, environment)
        execute(["git", "add", "."], root, environment)
        execute(["git", "commit", "-qm", "fixture"], root, environment)

        plan_process = execute(["dlo", "optimize", "--root", str(root), "--plan", "--json"], root, environment)
        plan = json.loads(plan_process.stdout)
        assert plan["status"] == "candidate"
        assert plan["candidate"]["kind"] == "manifest-first"
        candidate_id = plan["candidate"]["candidate_id"]

        result_process = execute(["dlo", "optimize", "--root", str(root), "--json"], root, environment)
        result = json.loads(result_process.stdout)
        assert result["status"] == "verified-applied"
        assert result["candidate"]["candidate_id"] == candidate_id
        assert result["applied"] is True
        assert all(result["verification"]["gates"].values())
        source = result["verification"]["benchmark"]["source_change"]
        assert source["absolute_improvement_seconds"] >= 0.25
        assert source["relative_improvement"] >= 0.10

        rewritten = (root / "Dockerfile").read_text(encoding="utf-8")
        assert rewritten.index('COPY ["requirements.txt", "./"]') < rewritten.index("RUN echo")
        assert rewritten.index("RUN echo") < rewritten.index("COPY . .")
        proof = json.loads(Path(result["proof_file"]).read_text(encoding="utf-8"))
        serialized_proof = json.dumps(proof)
        assert "python -m py_compile" not in serialized_proof
        assert "COPY . ." not in serialized_proof
        assert "patch" not in proof
        print(json.dumps({
            "status": "ok",
            "candidate_id": candidate_id,
            "control_median_seconds": source["control"]["median_seconds"],
            "candidate_median_seconds": source["candidate"]["median_seconds"],
            "verification_seconds": result["verification"]["benchmark"]["verification_seconds"],
            "break_even_deploys": result["verification"]["benchmark"]["estimated_break_even_deploys"],
        }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
