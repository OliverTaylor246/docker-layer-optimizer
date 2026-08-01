#!/usr/bin/env python3
"""Reproducible raw-Docker overhead and control-vs-optimized benchmark matrix."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import platform
import shutil
import statistics
import subprocess
import tempfile
import time
import uuid


CASES = {
    "python": {
        "files": {
            "requirements.txt": "packaging==24.2\n",
            "app.py": "from packaging.version import Version\nprint(Version('1.0'))\n",
        },
        "source": "app.py",
        "source_comment": "#",
        "dependency": "requirements.txt",
        "dependency_comment": "#",
        "control": "FROM python:3.12-alpine\nWORKDIR /app\nCOPY . .\nRUN pip install --no-cache-dir -r requirements.txt\nCMD [\"python\", \"app.py\"]\n",
        "optimized": "FROM python:3.12-alpine\nWORKDIR /app\nCOPY requirements.txt .\nRUN pip install --no-cache-dir -r requirements.txt\nCOPY app.py .\nCMD [\"python\", \"app.py\"]\n",
    },
    "node": {
        "files": {
            "package.json": '{"scripts":{"start":"node src.js"},"dependencies":{"is-number":"7.0.0"}}\n',
            "src.js": "console.log(require('is-number')(42));\n",
        },
        "source": "src.js",
        "source_comment": "//",
        "dependency": "package.json",
        "dependency_comment": None,
        "control": "FROM node:22-alpine\nWORKDIR /app\nCOPY . .\nRUN npm install --omit=dev\nCMD [\"node\", \"src.js\"]\n",
        "optimized": "FROM node:22-alpine\nWORKDIR /app\nCOPY package.json .\nRUN npm install --omit=dev\nCOPY src.js .\nCMD [\"node\", \"src.js\"]\n",
    },
    "go": {
        "files": {
            "go.mod": "module example.com/dlo-benchmark\n\ngo 1.23\n",
            "main.go": "package main\nimport \"fmt\"\nfunc main(){fmt.Println(\"hello\")}\n",
        },
        "source": "main.go",
        "source_comment": "//",
        "dependency": "go.mod",
        "dependency_comment": "//",
        "control": "FROM golang:1.24-alpine AS build\nWORKDIR /src\nCOPY . .\nRUN go mod download\nRUN go build -o /out/app .\nFROM alpine:3.21\nCOPY --from=build /out/app /app\nCMD [\"/app\"]\n",
        "optimized": "FROM golang:1.24-alpine AS build\nWORKDIR /src\nCOPY go.mod .\nRUN go mod download\nCOPY main.go .\nRUN go build -o /out/app .\nFROM alpine:3.21\nCOPY --from=build /out/app /app\nCMD [\"/app\"]\n",
    },
    "monorepo": {
        "files": {
            "requirements.txt": "packaging==24.2\n",
            "services/api.py": "from packaging.version import Version\nprint(Version('2.0'))\n",
            "services/worker.py": "print('worker')\n",
            "docs/guide.md": "ignored documentation\n",
            ".dockerignore": "docs/\n",
        },
        "source": "services/api.py",
        "source_comment": "#",
        "dependency": "requirements.txt",
        "dependency_comment": "#",
        "control": "FROM python:3.12-alpine\nWORKDIR /app\nCOPY . .\nRUN pip install --no-cache-dir -r requirements.txt\nCMD [\"python\", \"services/api.py\"]\n",
        "optimized": "FROM python:3.12-alpine\nWORKDIR /app\nCOPY requirements.txt .\nRUN pip install --no-cache-dir -r requirements.txt\nCOPY services/ services/\nCMD [\"python\", \"services/api.py\"]\n",
    },
}


def run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None, json_output: bool = False):
    started = time.perf_counter()
    process = subprocess.run(command, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    elapsed = time.perf_counter() - started
    if process.returncode != 0:
        raise RuntimeError(f"command failed ({process.returncode}): {' '.join(command)}\n{process.stdout}\n{process.stderr}")
    return elapsed, json.loads(process.stdout) if json_output else process.stdout


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(len(ordered) * fraction) - 1))]


def stats(values: list[float]) -> dict:
    return {
        "runs": len(values),
        "median_seconds": round(statistics.median(values), 6),
        "p95_seconds": round(percentile(values, 0.95), 6),
        "min_seconds": round(min(values), 6),
        "max_seconds": round(max(values), 6),
    }


def write_case(root: Path, case: dict, layout: str, salt: str) -> None:
    root.mkdir(parents=True)
    for relative, content in case["files"].items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    (root / "Dockerfile").write_text(case[layout], encoding="utf-8")
    for kind in ("source", "dependency"):
        path = root / case[kind]
        comment = case[f"{kind}_comment"]
        if comment is None:
            value = json.loads(path.read_text(encoding="utf-8"))
            value["dloBenchmarkRun"] = salt
            path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
        else:
            path.write_text(path.read_text(encoding="utf-8") + f"\n{comment} benchmark run {salt}\n", encoding="utf-8")


def edit_input(root: Path, case: dict, kind: str, index: int) -> None:
    relative = case[kind]
    path = root / relative
    comment = case[f"{kind}_comment"]
    if comment is None:
        value = json.loads(path.read_text(encoding="utf-8"))
        value["dloBenchmarkIteration"] = index
        path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    else:
        path.write_text(
            path.read_text(encoding="utf-8") + f"\n{comment} benchmark {kind} edit {index}\n",
            encoding="utf-8",
        )


def dlo_command() -> list[str]:
    executable = shutil.which("dlo")
    if not executable:
        raise RuntimeError("dlo is not installed; run `python -m pip install .` first")
    return [executable]


def benchmark_case(name: str, case: dict, iterations: int, workspace: Path) -> dict:
    control, optimized = workspace / f"{name}-control", workspace / f"{name}-optimized"
    salt = f"{name}-{uuid.uuid4().hex}"
    write_case(control, case, "control", salt)
    write_case(optimized, case, "optimized", salt)
    state = workspace / "state"
    environment = dict(os.environ, DLO_CACHE_DIR=str(state))
    command = dlo_command()

    tags = {"control": f"dlo-benchmark/{name}-control:latest", "optimized": f"dlo-benchmark/{name}-optimized:latest"}
    for layout, root in (("control", control), ("optimized", optimized)):
        run(command + ["build", "--root", str(root), "--tag", tags[layout], "--quiet", "--json"], cwd=root, env=environment, json_output=True)

    change_results = {}
    for kind in ("source", "dependency"):
        change_times: dict[str, list[float]] = {"control": [], "optimized": []}
        observations: dict[str, list[dict]] = {"control": [], "optimized": []}
        for index in range(iterations):
            for layout, root in (("control", control), ("optimized", optimized)):
                edit_input(root, case, kind, index)
                elapsed, observation = run(
                    command + ["build", "--root", str(root), "--tag", tags[layout], "--quiet", "--json"],
                    cwd=root, env=environment, json_output=True,
                )
                change_times[layout].append(elapsed)
                observations[layout].append({
                    "cached": observation["steps"]["cached"],
                    "rebuilt": observation["steps"]["rebuilt"],
                    "matching_diff_ids": observation["image"]["matching_diff_ids"],
                    "unmatched_diff_ids": observation["image"]["unmatched_diff_ids"],
                })
        control_median = statistics.median(change_times["control"])
        optimized_median = statistics.median(change_times["optimized"])
        change_results[f"{kind}_edit"] = {
            "control": stats(change_times["control"]),
            "optimized": stats(change_times["optimized"]),
            "median_savings_seconds": round(control_median - optimized_median, 6),
            "median_savings_percent": round((control_median - optimized_median) / control_median * 100, 2) if control_median else None,
            "observations": observations,
        }

    raw_times, dlo_times, dlo_overhead = [], [], []
    for _ in range(iterations):
        elapsed, _ = run([
            "docker", "buildx", "build", "--progress=quiet", "--load", "--tag", tags["optimized"], str(optimized),
        ], cwd=optimized)
        raw_times.append(elapsed)
        elapsed, observation = run(
            command + ["build", "--root", str(optimized), "--tag", tags["optimized"], "--quiet", "--json"],
            cwd=optimized, env=environment, json_output=True,
        )
        dlo_times.append(elapsed)
        dlo_overhead.append(float(observation["overhead"]["non_build_seconds"]))

    return {
        **change_results,
        "no_op_overhead": {
            "raw_docker": stats(raw_times),
            "dlo_wall": stats(dlo_times),
            "dlo_reported_non_build": stats(dlo_overhead),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", default=",".join(CASES), help="comma-separated: python,node,go,monorepo")
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("--iterations must be positive")
    selected = [name.strip() for name in args.cases.split(",") if name.strip()]
    unknown = set(selected) - set(CASES)
    if unknown:
        parser.error(f"unknown cases: {', '.join(sorted(unknown))}")
    with tempfile.TemporaryDirectory(prefix="dlo-benchmarks-") as directory:
        root = Path(directory)
        results = {
            "schema_version": 1,
            "environment": {
                "platform": platform.platform(),
                "machine": platform.machine(),
                "python": platform.python_version(),
                "docker": subprocess.run(["docker", "version", "--format", "{{.Client.Version}}/{{.Server.Version}}"], text=True, stdout=subprocess.PIPE, check=True).stdout.strip(),
                "buildx": subprocess.run(["docker", "buildx", "version"], text=True, stdout=subprocess.PIPE, check=True).stdout.strip(),
            },
            "iterations": args.iterations,
            "cases": {},
        }
        try:
            for name in selected:
                results["cases"][name] = benchmark_case(name, CASES[name], args.iterations, root)
        finally:
            for name in selected:
                for layout in ("control", "optimized"):
                    subprocess.run(
                        ["docker", "image", "rm", f"dlo-benchmark/{name}-{layout}:latest"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                    )
    payload = json.dumps(results, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
