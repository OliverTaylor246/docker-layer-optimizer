#!/usr/bin/env python3
"""History-aware Docker layer cache analyzer with local, privacy-safe observations."""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import fnmatch
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import statistics
import subprocess
import sys
from typing import Iterable, Sequence


STATE_DIR = "docker-layer-optimizer"
EVENTS_FILE = "events.jsonl"
HEADER = "__DLO_COMMIT__"

MANIFEST_PATTERNS = (
    "requirements*.txt", "pyproject.toml", "poetry.lock", "uv.lock", "Pipfile", "Pipfile.lock",
    "package.json", "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml",
    "go.mod", "go.sum", "Cargo.toml", "Cargo.lock", "Package.swift", "Package.resolved",
    "Gemfile", "Gemfile.lock", "composer.json", "composer.lock", "pom.xml", "build.gradle*",
)

DEPENDENCY_TERMS = (
    "apt-get install", "apk add", "dnf install", "yum install", "pip install", "pip3 install",
    "uv sync", "uv pip", "poetry install", "npm ci", "npm install", "pnpm install", "yarn install",
    "bundle install", "composer install", "go mod download", "cargo fetch", "swift package resolve",
)
BUILD_TERMS = (
    "cargo build", "go build", "swift build", "npm run build", "pnpm build", "yarn build",
    "gradle build", "mvn package", "make", "cmake --build", "dotnet publish",
)


@dataclasses.dataclass(frozen=True)
class Instruction:
    command: str
    args: str
    line: int
    raw: str
    stage: int


def git(root: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args], text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=False,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"git {' '.join(args)} failed")
    return proc.stdout


def project_root(value: str) -> Path:
    candidate = Path(value).expanduser().resolve()
    if not candidate.is_dir():
        raise FileNotFoundError(f"project root is not a directory: {candidate}")
    return candidate


def dockerfile_path(root: Path, value: str | None) -> Path:
    if value:
        path = Path(value)
        return (path if path.is_absolute() else root / path).resolve()
    for name in ("Dockerfile", "Containerfile"):
        path = root / name
        if path.is_file():
            return path
    raise FileNotFoundError("no Dockerfile or Containerfile found; pass --dockerfile")


def parse_dockerfile(path: Path) -> list[Instruction]:
    lines = path.read_text(encoding="utf-8").splitlines()
    instructions: list[Instruction] = []
    stage = -1
    index = 0
    while index < len(lines):
        start = index
        parts = [lines[index]]
        while parts[-1].rstrip().endswith("\\") and index + 1 < len(lines):
            parts[-1] = parts[-1].rstrip()[:-1]
            index += 1
            parts.append(lines[index].strip())
        index += 1
        logical = " ".join(part.strip() for part in parts).strip()
        if not logical or logical.startswith("#") or logical.lower().startswith("# syntax="):
            continue
        match = re.match(r"([A-Za-z]+)\s+(.*)$", logical, re.DOTALL)
        if not match:
            continue
        command, args = match.group(1).upper(), match.group(2).strip()
        if command == "FROM":
            stage += 1
        instructions.append(Instruction(command, args, start + 1, logical, max(stage, 0)))
    return instructions


def parse_copy_sources(instruction: Instruction) -> list[str] | None:
    if instruction.command not in {"COPY", "ADD"}:
        return None
    remaining = instruction.args.lstrip()
    while remaining.startswith("--"):
        flag, separator, remaining = remaining.partition(" ")
        if flag == "--from" or flag.startswith("--from="):
            return None
        if not separator:
            return None
        remaining = remaining.lstrip()
    if remaining.startswith("["):
        try:
            values = json.loads(remaining)
            return [str(item) for item in values[:-1]] if len(values) >= 2 else None
        except (json.JSONDecodeError, TypeError):
            return None
    try:
        tokens = shlex.split(remaining, posix=True)
    except ValueError:
        return None
    if not tokens:
        return None
    return tokens[:-1] if len(tokens) >= 2 else None


def tracked_files(root: Path) -> list[str]:
    raw = git(root, "ls-files", "-z", check=False)
    return sorted(item for item in raw.split("\0") if item)


def commit_history(root: Path, limit: int) -> list[set[str]]:
    prefix = git(root, "rev-parse", "--show-prefix", check=False).strip()
    raw = git(
        root, "log", f"-n{limit}", f"--format={HEADER}%H", "--name-only", "--no-renames", "--", ".",
        check=False,
    )
    commits: list[set[str]] = []
    current: set[str] | None = None
    for line in raw.splitlines():
        if line.startswith(HEADER):
            if current is not None:
                commits.append(current)
            current = set()
        elif current is not None and line.strip():
            path = line.strip()
            if prefix:
                if not path.startswith(prefix):
                    continue
                path = path[len(prefix):]
            current.add(path)
    if current is not None:
        commits.append(current)
    return commits


def state_path(root: Path) -> Path:
    raw = git(root, "rev-parse", "--git-path", STATE_DIR, check=False).strip()
    if raw:
        state = Path(raw)
        return (state if state.is_absolute() else root / state).resolve()
    return root / f".{STATE_DIR}"


def load_events(root: Path, limit: int = 500) -> list[dict]:
    path = state_path(root) / EVENTS_FILE
    if not path.is_file():
        return []
    events: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                events.append(value)
        except json.JSONDecodeError:
            continue
    return events


def event_change_sets(events: Sequence[dict]) -> list[set[str]]:
    result = []
    for event in reversed(events):
        paths = event.get("changed_paths", [])
        if isinstance(paths, list):
            result.append({str(path) for path in paths})
    return result


def decay_weights(length: int, half_life: float) -> list[float]:
    return [math.pow(0.5, index / half_life) for index in range(length)]


def probability(change_sets: Sequence[set[str]], matched: set[str], half_life: float) -> float:
    if not change_sets or not matched:
        return 0.0
    weights = decay_weights(len(change_sets), half_life)
    numerator = sum(weight for weight, changed in zip(weights, change_sets) if changed & matched)
    return numerator / sum(weights)


def matches_source(source: str, files: Sequence[str]) -> set[str]:
    normalized = source.replace("\\", "/").lstrip("./")
    if source in {".", "./"} or normalized == "":
        return set(files)
    if "$" in source:
        return set()
    if any(char in normalized for char in "*?["):
        return {path for path in files if fnmatch.fnmatch(path, normalized)}
    prefix = normalized.rstrip("/") + "/"
    return {path for path in files if path == normalized or path.startswith(prefix)}


def manifest_files(files: Sequence[str]) -> list[str]:
    return sorted(
        path for path in files
        if any(fnmatch.fnmatch(Path(path).name, pattern) for pattern in MANIFEST_PATTERNS)
    )


def instruction_cost(instruction: Instruction) -> float:
    if instruction.command == "RUN":
        lower = instruction.args.lower()
        if any(term in lower for term in DEPENDENCY_TERMS):
            return 10.0
        if any(term in lower for term in BUILD_TERMS):
            return 8.0
        return 3.0
    if instruction.command in {"COPY", "ADD"}:
        return 1.0
    if instruction.command in {"FROM", "ARG"}:
        return 0.0
    return 0.5


def downstream_cost(instructions: Sequence[Instruction], index: int) -> float:
    stage = instructions[index].stage
    return sum(
        instruction_cost(item) for item in instructions[index:]
        if item.stage == stage
    )


def top_level(path: str) -> str:
    return path.split("/", 1)[0] if "/" in path else "(root files)"


def area_stats(change_sets: Sequence[set[str]], files: Sequence[str]) -> list[dict]:
    groups: dict[str, set[str]] = {}
    for path in files:
        groups.setdefault(top_level(path), set()).add(path)
    rows = [
        {"area": name, "change_likelihood": round(probability(change_sets, paths, 30.0), 4), "files": len(paths)}
        for name, paths in groups.items()
    ]
    return sorted(rows, key=lambda row: (-row["change_likelihood"], row["area"]))[:12]


def cochange_pairs(change_sets: Sequence[set[str]]) -> list[dict]:
    group_sets = [{top_level(path) for path in changed} for changed in change_sets]
    names = sorted(set().union(*group_sets)) if group_sets else []
    pairs: list[dict] = []
    for left_index, left in enumerate(names):
        for right in names[left_index + 1:]:
            both = sum(left in groups and right in groups for groups in group_sets)
            either = sum(left in groups or right in groups for groups in group_sets)
            if both >= 2 and either:
                score = both / either
                if score >= 0.45:
                    pairs.append({"areas": [left, right], "similarity": round(score, 4), "commits": both})
    return sorted(pairs, key=lambda row: (-row["similarity"], -row["commits"]))[:8]


def observed_metrics(events: Sequence[dict]) -> dict:
    durations = [float(event["duration_seconds"]) for event in events if event.get("duration_seconds") is not None]
    pushed = [int(event["bytes_pushed"]) for event in events if event.get("bytes_pushed") is not None]
    return {
        "median_duration_seconds": round(statistics.median(durations), 3) if durations else None,
        "median_bytes_pushed": int(statistics.median(pushed)) if pushed else None,
    }


def analyze(root: Path, dockerfile: Path, commit_limit: int) -> dict:
    instructions = parse_dockerfile(dockerfile)
    files = tracked_files(root)
    commits = commit_history(root, commit_limit)
    events = load_events(root)
    event_sets = event_change_sets(events)
    manifests = manifest_files(files)
    layers: list[dict] = []

    for index, instruction in enumerate(instructions):
        sources = parse_copy_sources(instruction)
        if sources is None:
            continue
        matched = set().union(*(matches_source(source, files) for source in sources)) if sources else set()
        git_probability = probability(commits, matched, 30.0)
        local_probability = probability(event_sets, matched, 20.0)
        if commits and event_sets:
            likelihood = 0.7 * git_probability + 0.3 * local_probability
        elif commits:
            likelihood = git_probability
        elif event_sets:
            likelihood = local_probability
        else:
            likelihood = 0.0
        cost = downstream_cost(instructions, index)
        layers.append({
            "line": instruction.line,
            "stage": instruction.stage,
            "instruction": instruction.raw,
            "sources": sources,
            "matched_files": len(matched),
            "change_likelihood": round(likelihood, 4),
            "git_likelihood": round(git_probability, 4),
            "local_likelihood": round(local_probability, 4) if event_sets else None,
            "downstream_cost_units": round(cost, 2),
            "expected_rebuild_cost": round(likelihood * cost, 3),
        })

    recommendations: list[dict] = []
    for layer in layers:
        broad = any(source in {".", "./"} for source in layer["sources"])
        line = layer["line"]
        instruction_index = next(i for i, item in enumerate(instructions) if item.line == line)
        later = [item for item in instructions[instruction_index + 1:] if item.stage == instructions[instruction_index].stage]
        dependency_run = next(
            (item for item in later if item.command == "RUN" and any(term in item.args.lower() for term in DEPENDENCY_TERMS)),
            None,
        )
        if broad and dependency_run and manifests:
            recommendations.append({
                "priority": "high",
                "line": line,
                "kind": "split-dependency-inputs",
                "message": (
                    f"Split dependency manifests into an earlier COPY, run dependency installation at line "
                    f"{dependency_run.line}, then copy volatile source. Candidate manifests: {', '.join(manifests[:8])}."
                ),
            })
        if broad and not (root / ".dockerignore").is_file():
            recommendations.append({
                "priority": "medium", "line": line, "kind": "add-dockerignore",
                "message": "Add a .dockerignore so generated files and local state do not enter the build context.",
            })

    same_stage_layers = sorted(layers, key=lambda item: item["line"])
    for previous, current in zip(same_stage_layers, same_stage_layers[1:]):
        if previous["stage"] == current["stage"] and previous["change_likelihood"] > current["change_likelihood"] + 0.2:
            between = [
                item for item in instructions
                if previous["line"] < item.line < current["line"] and item.command == "RUN"
            ]
            if not between:
                recommendations.append({
                    "priority": "low", "line": previous["line"], "kind": "review-copy-order",
                    "message": (
                        f"Review whether the COPY at line {current['line']} can precede the more volatile COPY at "
                        f"line {previous['line']}; only reorder if their destinations and consumers are independent."
                    ),
                })

    if not recommendations:
        recommendations.append({
            "priority": "info", "line": None, "kind": "measure",
            "message": "No obvious static layer split was found. Record representative warm builds to replace heuristic cost with evidence.",
        })

    return {
        "schema_version": 1,
        "project_root": str(root),
        "dockerfile": str(dockerfile.relative_to(root) if dockerfile.is_relative_to(root) else dockerfile),
        "evidence": {"commits": len(commits), "local_observations": len(events), **observed_metrics(events)},
        "volatile_areas": area_stats(commits, files),
        "cochange_pairs": cochange_pairs(commits),
        "layers": sorted(layers, key=lambda item: -item["expected_rebuild_cost"]),
        "recommendations": recommendations,
    }


def current_changes(root: Path) -> list[str]:
    prefix = git(root, "rev-parse", "--show-prefix", check=False).strip()
    changed = git(root, "diff", "--name-only", "-z", "HEAD", check=False).split("\0")
    untracked = git(root, "ls-files", "--others", "--exclude-standard", "-z", check=False).split("\0")
    result = set()
    for item in changed + untracked:
        if not item:
            continue
        if prefix and item.startswith(prefix):
            item = item[len(prefix):]
        elif prefix and "/" in item:
            continue
        result.add(item)
    return sorted(result)


def clean_tags(tags: Iterable[str]) -> list[str]:
    result = []
    for tag in tags:
        normalized = re.sub(r"[^a-z0-9-]+", "-", tag.lower()).strip("-")[:40]
        if normalized:
            result.append(normalized)
    return sorted(set(result))


def record(args: argparse.Namespace) -> dict:
    root = project_root(args.root)
    paths = list(args.changed or [])
    if args.from_git:
        paths.extend(current_changes(root))
    normalized_paths = []
    for value in paths:
        path = Path(value)
        absolute = (path if path.is_absolute() else root / path).resolve()
        try:
            normalized_paths.append(absolute.relative_to(root).as_posix())
        except ValueError as exc:
            raise ValueError(f"changed path is outside project root: {value}") from exc
    commit = git(root, "rev-parse", "HEAD", check=False).strip() or None
    dockerfile = None
    try:
        df = dockerfile_path(root, args.dockerfile)
        dockerfile = {
            "path": df.relative_to(root).as_posix() if df.is_relative_to(root) else str(df),
            "sha256": hashlib.sha256(df.read_bytes()).hexdigest(),
        }
    except FileNotFoundError:
        pass
    event = {
        "schema_version": 1,
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        "kind": args.kind,
        "status": args.status,
        "commit": commit,
        "changed_paths": sorted(set(normalized_paths)),
        "tags": clean_tags(args.tag or []),
        "duration_seconds": args.duration,
        "bytes_pushed": args.bytes_pushed,
        "invalidated_from": args.invalidated_from,
        "dockerfile": dockerfile,
    }
    directory = state_path(root)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / EVENTS_FILE).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")
    return {"recorded": True, "state_file": str(directory / EVENTS_FILE), "event": event}


def render(report: dict) -> str:
    evidence = report["evidence"]
    lines = [
        f"Docker layer analysis: {report['dockerfile']}",
        f"Evidence: {evidence['commits']} commits, {evidence['local_observations']} local observations",
    ]
    if evidence["median_duration_seconds"] is not None:
        lines.append(f"Observed median duration: {evidence['median_duration_seconds']}s")
    lines.append("\nHighest-risk context layers:")
    if not report["layers"]:
        lines.append("  (no build-context COPY or ADD instructions found)")
    for layer in report["layers"][:8]:
        lines.append(
            f"  line {layer['line']}: risk {layer['expected_rebuild_cost']:.3f}, "
            f"change {layer['change_likelihood']:.1%}, downstream cost {layer['downstream_cost_units']:.1f}"
        )
        lines.append(f"    {layer['instruction']}")
    lines.append("\nMost volatile project areas:")
    for area in report["volatile_areas"][:8]:
        lines.append(f"  {area['area']}: {area['change_likelihood']:.1%} ({area['files']} tracked files)")
    lines.append("\nRecommendations:")
    for index, recommendation in enumerate(report["recommendations"], 1):
        lines.append(f"  {index}. [{recommendation['priority']}] {recommendation['message']}")
    if not evidence["local_observations"]:
        lines.append("\nLearning profile is empty. Use the record command after relevant tasks and builds.")
    return "\n".join(lines)


def parser() -> argparse.ArgumentParser:
    root_parser = argparse.ArgumentParser(description=__doc__)
    subparsers = root_parser.add_subparsers(dest="command", required=True)
    analyze_parser = subparsers.add_parser("analyze", help="score Docker context layers")
    analyze_parser.add_argument("--root", default=".")
    analyze_parser.add_argument("--dockerfile")
    analyze_parser.add_argument("--commits", type=int, default=200)
    analyze_parser.add_argument("--json", action="store_true")

    record_parser = subparsers.add_parser("record", help="append a privacy-safe local observation")
    record_parser.add_argument("--root", default=".")
    record_parser.add_argument("--dockerfile")
    record_parser.add_argument("--kind", choices=("task", "build", "deploy"), required=True)
    record_parser.add_argument("--status", choices=("success", "failure", "partial"), default="success")
    record_parser.add_argument("--duration", type=float)
    record_parser.add_argument("--bytes-pushed", type=int)
    record_parser.add_argument("--invalidated-from", type=int)
    record_parser.add_argument("--tag", action="append", default=[])
    record_parser.add_argument("--changed", action="append", default=[])
    record_parser.add_argument("--from-git", action="store_true")
    record_parser.add_argument("--json", action="store_true")
    return root_parser


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "analyze":
            root = project_root(args.root)
            report = analyze(root, dockerfile_path(root, args.dockerfile), args.commits)
            print(json.dumps(report, indent=2, sort_keys=True) if args.json else render(report))
        else:
            result = record(args)
            print(json.dumps(result, indent=2, sort_keys=True) if args.json else f"Recorded observation in {result['state_file']}")
        return 0
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
