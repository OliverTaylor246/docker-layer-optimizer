"""Candidate planning, proof, and safe application for agent-driven optimization."""

from __future__ import annotations

import dataclasses
import datetime as dt
import difflib
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import statistics
import subprocess
import tempfile
import time
from typing import Callable, Iterable, Sequence


PROTECTED_DOCKERFILE_COMMANDS = {
    "FROM", "CMD", "ENTRYPOINT", "USER", "EXPOSE", "HEALTHCHECK", "STOPSIGNAL", "VOLUME",
}
TEXT_COMMENT_PREFIXES = {
    ".c": "//", ".cc": "//", ".cpp": "//", ".css": "/*", ".go": "//", ".h": "//",
    ".hpp": "//", ".java": "//", ".js": "//", ".jsx": "//", ".kt": "//", ".mjs": "//",
    ".php": "//", ".py": "#", ".rb": "#", ".rs": "//", ".sh": "#", ".swift": "//",
    ".ts": "//", ".tsx": "//",
}
MANIFEST_COMMENT_PREFIXES = {
    ".toml": "#", ".txt": "#", ".yaml": "#", ".yml": "#",
}
ALLOWED_COMPOSE_NAMES = {
    "compose.yml", "compose.yaml", "docker-compose.yml", "docker-compose.yaml",
}


@dataclasses.dataclass(frozen=True)
class Candidate:
    candidate_id: str
    origin: str
    kind: str
    patch: str
    affected_paths: tuple[str, ...]
    protected_changes: tuple[str, ...]
    rationale: str


@dataclasses.dataclass(frozen=True)
class Settings:
    trials: int = 3
    budget_seconds: float = 600.0
    min_relative_improvement: float = 0.10
    min_absolute_seconds: float = 0.5
    max_relative_regression: float = 0.10
    max_absolute_regression_seconds: float = 0.5
    payback_deploys: float = 20.0
    source_path: str | None = None
    verification_commands: tuple[str, ...] = ()
    platform: str | None = None
    target: str | None = None
    builder: str | None = None
    build_args: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class BuildResult:
    return_code: int
    duration_seconds: float
    cached_steps: int
    rebuilt_steps: int
    failed_steps: int
    error_kind: str | None = None


BuildRunner = Callable[[Path, Path, str, Settings, float], BuildResult]
CommandRunner = Callable[[str, Path, dict[str, str], float], int]


class VerificationFailure(RuntimeError):
    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _relative_dockerfile(root: Path, dockerfile: Path) -> Path:
    try:
        return dockerfile.relative_to(root)
    except ValueError as exc:
        raise ValueError("optimization requires the Dockerfile to be inside the project root") from exc


def _copy_destination(instruction, optimizer) -> str | None:
    remaining = instruction.args.lstrip()
    if remaining.startswith("--"):
        return None
    if remaining.startswith("["):
        try:
            values = json.loads(remaining)
        except (json.JSONDecodeError, TypeError):
            return None
        return str(values[-1]) if len(values) >= 2 else None
    try:
        values = shlex.split(remaining, posix=True)
    except ValueError:
        return None
    return values[-1] if len(values) >= 2 else None


def _instruction_end(lines: Sequence[str], start_line: int) -> int:
    index = start_line - 1
    while index < len(lines) and lines[index].rstrip().endswith("\\"):
        index += 1
    return min(index + 1, len(lines))


def _manifest_first_text(root: Path, dockerfile: Path, optimizer) -> tuple[str, str] | None:
    """Return a conservative manifest-first rewrite and rationale, if one is obvious."""

    instructions = optimizer.parse_dockerfile(dockerfile)
    from build_observer import filter_context_files
    files = filter_context_files(root, dockerfile, optimizer.tracked_files(root))
    manifests = optimizer.manifest_files(files)
    if not manifests:
        return None
    original = dockerfile.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)
    for index, instruction in enumerate(instructions[:-1]):
        sources = optimizer.parse_copy_sources(instruction)
        if sources not in (["."], ["./"]):
            continue
        dependency = instructions[index + 1]
        if dependency.stage != instruction.stage or dependency.command != "RUN":
            continue
        if not any(term in dependency.args.lower() for term in optimizer.DEPENDENCY_TERMS):
            continue
        if "\\" in lines[instruction.line - 1]:
            continue
        destination = _copy_destination(instruction, optimizer)
        if not destination:
            continue
        destination = "./" if destination in {".", "./"} else destination.rstrip("/") + "/"
        source_values = ", ".join(json.dumps(path) for path in manifests)
        manifest_copy = f"COPY [{source_values}, {json.dumps(destination)}]\n"
        copy_index = instruction.line - 1
        dependency_end = _instruction_end(lines, dependency.line)
        replacement = (
            lines[:copy_index]
            + [manifest_copy]
            + lines[copy_index + 1:dependency_end]
            + [lines[copy_index]]
            + lines[dependency_end:]
        )
        candidate = "".join(replacement)
        if candidate == original:
            continue
        rationale = (
            f"Copy {len(manifests)} dependency manifest(s) before the dependency installation at line "
            f"{dependency.line}, then copy volatile source so source-only edits can reuse that work."
        )
        return candidate, rationale
    return None


def _unified_diff(relative: Path, before: str, after: str) -> str:
    path = relative.as_posix()
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}",
    ))


def affected_paths(patch: str) -> tuple[str, ...]:
    paths: set[str] = set()
    for line in patch.splitlines():
        if not line.startswith("+++ "):
            continue
        value = line[4:].split("\t", 1)[0].strip()
        if value == "/dev/null":
            continue
        if value.startswith("b/"):
            value = value[2:]
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or not value:
            raise ValueError(f"candidate patch contains an unsafe path: {value}")
        paths.add(path.as_posix())
    if not paths:
        raise ValueError("candidate patch does not contain a supported unified diff")
    return tuple(sorted(paths))


def _is_docker_build_path(value: str) -> bool:
    path = Path(value)
    name = path.name
    return (
        name in {"Dockerfile", "Containerfile", ".dockerignore"}
        or name.startswith("Dockerfile.")
        or name.startswith("Containerfile.")
        or name.endswith(".dockerignore")
        or name in ALLOWED_COMPOSE_NAMES
    )


def _apply_patch(root: Path, patch: str, check: bool = False) -> None:
    command = ["git", "apply", "--whitespace=nowarn"]
    if check:
        command.append("--check")
    process = subprocess.run(
        command, cwd=root, input=patch, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=False,
    )
    if process.returncode != 0:
        raise ValueError(process.stderr.strip() or "candidate patch could not be applied")


def _protected_changes(root: Path, dockerfile: Path, patch: str, paths: Sequence[str], optimizer) -> tuple[str, ...]:
    changes: list[str] = []
    disallowed = [path for path in paths if not _is_docker_build_path(path)]
    if disallowed:
        changes.append("outside-docker-build-scope:" + ",".join(disallowed))
    relative = _relative_dockerfile(root, dockerfile).as_posix()
    if relative not in paths:
        return tuple(changes)
    with tempfile.TemporaryDirectory(prefix="dlo-protected-") as directory:
        snapshot = Path(directory)
        _copy_tree(root, snapshot)
        _apply_patch(snapshot, patch)
        candidate_dockerfile = snapshot / relative
        if not candidate_dockerfile.is_file():
            changes.append("dockerfile-removed")
            return tuple(changes)
        before = [
            (item.command, item.args) for item in optimizer.parse_dockerfile(dockerfile)
            if item.command in PROTECTED_DOCKERFILE_COMMANDS
        ]
        after = [
            (item.command, item.args) for item in optimizer.parse_dockerfile(candidate_dockerfile)
            if item.command in PROTECTED_DOCKERFILE_COMMANDS
        ]
        if before != after:
            changes.append("protected-dockerfile-semantics")
    return tuple(changes)


def candidate_from_patch(root: Path, dockerfile: Path, patch: str, optimizer, origin: str = "agent") -> Candidate:
    paths = affected_paths(patch)
    _apply_patch(root, patch, check=True)
    candidate_id = _sha256(patch.encode("utf-8"))[:20]
    return Candidate(
        candidate_id=candidate_id,
        origin=origin,
        kind="agent-patch" if origin == "agent" else "manifest-first",
        patch=patch,
        affected_paths=paths,
        protected_changes=_protected_changes(root, dockerfile, patch, paths, optimizer),
        rationale=(
            "Agent-supplied Docker build candidate; DLO has not inferred its semantic intent."
            if origin == "agent" else
            "Move volatile source copying after dependency installation."
        ),
    )


def generate_candidate(root: Path, dockerfile: Path, optimizer) -> Candidate | None:
    generated = _manifest_first_text(root, dockerfile, optimizer)
    if not generated:
        return None
    after, rationale = generated
    relative = _relative_dockerfile(root, dockerfile)
    patch = _unified_diff(relative, dockerfile.read_text(encoding="utf-8"), after)
    candidate = candidate_from_patch(root, dockerfile, patch, optimizer, origin="builtin")
    return dataclasses.replace(candidate, rationale=rationale)


def plan(root: Path, dockerfile: Path, optimizer, patch: str | None = None) -> dict:
    report = optimizer.analyze(root, dockerfile, 200)
    candidate = candidate_from_patch(root, dockerfile, patch, optimizer) if patch else generate_candidate(root, dockerfile, optimizer)
    result = {
        "schema_version": 1,
        "tool_version": optimizer.VERSION,
        "kind": "optimization_plan",
        "status": "candidate" if candidate else "no-candidate",
        "project_root": str(root),
        "dockerfile": _relative_dockerfile(root, dockerfile).as_posix(),
        "evidence": report["evidence"],
        "optimization_signal": {
            "max_change_likelihood": max(
                (float(layer["change_likelihood"]) for layer in report["layers"]), default=0.0,
            ),
            "max_expected_rebuild_cost": max(
                (float(layer["expected_rebuild_cost"]) for layer in report["layers"]), default=0.0,
            ),
        },
        "candidate": dataclasses.asdict(candidate) if candidate else None,
        "next_action": (
            "Run `dlo optimize` with a verification contract to benchmark this candidate."
            if candidate else
            "No conservative built-in rewrite was found; an agent may submit a unified diff with --candidate."
        ),
    }
    return result


def _load_config(root: Path) -> dict:
    path = root / ".dlo.yml"
    if not path.is_file():
        return {}
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("reading .dlo.yml requires PyYAML; reinstall docker-layer-optimizer") from exc
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid .dlo.yml: {exc}") from exc
    if value is None:
        return {}
    if not isinstance(value, dict) or value.get("version", 1) != 1:
        raise ValueError(".dlo.yml must be a mapping with version: 1")
    return value


def settings_from_args(root: Path, args) -> Settings:
    config = _load_config(root)
    benchmark = config.get("benchmark") or {}
    verification = config.get("verification") or {}
    if not isinstance(benchmark, dict) or not isinstance(verification, dict):
        raise ValueError(".dlo.yml benchmark and verification values must be mappings")
    configured_commands = verification.get("commands") or []
    if not isinstance(configured_commands, list) or not all(isinstance(value, str) for value in configured_commands):
        raise ValueError(".dlo.yml verification.commands must be a list of command strings")
    commands = tuple(configured_commands) + tuple(args.test or []) + tuple(args.smoke_test or [])
    trials = args.trials if args.trials is not None else int(benchmark.get("trials", 3))
    if trials < 3:
        raise ValueError("optimization requires at least three paired trials")
    settings = Settings(
        trials=trials,
        budget_seconds=args.budget if args.budget is not None else float(benchmark.get("budget_seconds", 600)),
        min_relative_improvement=(
            args.min_relative_improvement if args.min_relative_improvement is not None
            else float(benchmark.get("min_relative_improvement", 0.10))
        ),
        min_absolute_seconds=(
            args.min_absolute_improvement if args.min_absolute_improvement is not None
            else float(benchmark.get("min_absolute_seconds", 0.5))
        ),
        max_relative_regression=(
            args.max_relative_regression if args.max_relative_regression is not None
            else float(benchmark.get("max_relative_regression", 0.10))
        ),
        max_absolute_regression_seconds=(
            args.max_absolute_regression if args.max_absolute_regression is not None
            else float(benchmark.get("max_absolute_regression_seconds", 0.5))
        ),
        payback_deploys=(
            args.payback_deploys if args.payback_deploys is not None
            else float(benchmark.get("payback_deploys", 20))
        ),
        source_path=args.source_path or benchmark.get("source_path"),
        verification_commands=commands,
        platform=args.platform,
        target=args.target,
        builder=args.builder,
        build_args=tuple(args.build_arg or []),
    )
    if settings.budget_seconds <= 0:
        raise ValueError("optimization budget must be positive")
    if not 0 < settings.min_relative_improvement < 1:
        raise ValueError("minimum relative improvement must be between 0 and 1")
    if settings.min_absolute_seconds < 0:
        raise ValueError("minimum absolute improvement cannot be negative")
    if not 0 <= settings.max_relative_regression < 1:
        raise ValueError("maximum relative regression must be between 0 and 1")
    if settings.max_absolute_regression_seconds < 0:
        raise ValueError("maximum absolute regression cannot be negative")
    if settings.payback_deploys <= 0:
        raise ValueError("payback deployment limit must be positive")
    return settings


def _copy_tree(source: Path, destination: Path) -> None:
    def ignore(_directory: str, names: list[str]) -> set[str]:
        return {".git"} & set(names)
    shutil.copytree(source, destination, symlinks=True, ignore=ignore, dirs_exist_ok=True)


def _file_hash(path: Path) -> str | None:
    if not path.exists():
        return None
    if path.is_symlink():
        return _sha256(os.readlink(path).encode("utf-8", errors="surrogateescape"))
    if not path.is_file():
        return "non-regular"
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _preimages(root: Path, paths: Iterable[str]) -> dict[str, str | None]:
    return {path: _file_hash(root / path) for path in paths}


def _assert_preimages(root: Path, expected: dict[str, str | None]) -> None:
    changed = [path for path, digest in expected.items() if _file_hash(root / path) != digest]
    if changed:
        raise RuntimeError("candidate is stale; affected files changed during verification: " + ", ".join(changed))


def _safe_source_path(root: Path, configured: str | None, optimizer) -> str | None:
    if configured:
        candidate = Path(configured)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("benchmark source_path must stay inside the project root")
        path = root / candidate
        if not path.is_file() or path.suffix.lower() not in TEXT_COMMENT_PREFIXES:
            raise ValueError("benchmark source_path must name a supported text source file")
        return candidate.as_posix()
    manifests = set(optimizer.manifest_files(optimizer.tracked_files(root)))
    candidates = [
        path for path in optimizer.tracked_files(root)
        if path not in manifests and Path(path).suffix.lower() in TEXT_COMMENT_PREFIXES
        and Path(path).name not in {"Dockerfile", "Containerfile"}
    ]
    preferred = ("app.", "main.", "server.", "index.")
    candidates.sort(key=lambda value: (not Path(value).name.startswith(preferred), len(Path(value).parts), value))
    return candidates[0] if candidates else None


def _safe_manifest_path(root: Path, optimizer) -> str | None:
    manifests = optimizer.manifest_files(optimizer.tracked_files(root))
    return next((path for path in manifests if (root / path).is_file()), None)


def _mutate(path: Path, marker: str, manifest: bool = False) -> None:
    suffix = path.suffix.lower()
    if manifest and suffix in {".json", ".lock"}:
        addition = "\n"
    else:
        prefix = (MANIFEST_COMMENT_PREFIXES if manifest else TEXT_COMMENT_PREFIXES).get(suffix, "#")
        addition = f"\n{prefix} dlo benchmark {marker}"
        if prefix == "/*":
            addition += " */"
        addition += "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(addition)


def _nearest_rank_p95(values: Sequence[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def _stats(values: Sequence[BuildResult]) -> dict:
    durations = [value.duration_seconds for value in values]
    return {
        "runs": len(values),
        "median_seconds": round(statistics.median(durations), 3),
        "p95_seconds": round(_nearest_rank_p95(durations), 3),
        "median_cached_steps": round(statistics.median(value.cached_steps for value in values), 1),
        "median_rebuilt_steps": round(statistics.median(value.rebuilt_steps for value in values), 1),
    }


def _run_command(command: str, root: Path, environment: dict[str, str], deadline: float) -> int:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("optimization budget exhausted")
    try:
        process = subprocess.run(
            command, cwd=root, env=environment, shell=True, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=remaining, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("verification command exceeded the optimization budget") from exc
    return process.returncode


def _run_docker_build(root: Path, dockerfile: Path, tag: str, settings: Settings, deadline: float) -> BuildResult:
    from build_observer import RawJsonProgressParser
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("optimization budget exhausted")
    command = [
        "docker", "buildx", "build", "--progress=rawjson", "--load",
        "--file", str(dockerfile), "--tag", tag,
    ]
    for flag, value in (("--platform", settings.platform), ("--target", settings.target), ("--builder", settings.builder)):
        if value:
            command.extend([flag, value])
    for value in settings.build_args:
        command.extend(["--build-arg", value])
    command.append(str(root))
    parser = RawJsonProgressParser()
    started = time.monotonic()
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as output:
        try:
            process = subprocess.Popen(command, cwd=root, text=True, stdout=output, stderr=subprocess.STDOUT)
        except FileNotFoundError as exc:
            raise RuntimeError("docker was not found; install Docker with Buildx before verification") from exc
        try:
            return_code = process.wait(timeout=max(0.001, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as exc:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise TimeoutError("Docker build exceeded the optimization budget") from exc
        output.seek(0)
        for line in output:
            parser.feed(line)
    summary = parser.summary()
    return BuildResult(
        return_code=return_code,
        duration_seconds=round(time.monotonic() - started, 3),
        cached_steps=summary["cached"],
        rebuilt_steps=summary["rebuilt"],
        failed_steps=summary["failed"],
        error_kind=None if return_code == 0 else "docker-build-failed",
    )


def _regression_allowed(control: float, candidate: float, settings: Settings) -> bool:
    tolerance = max(settings.max_absolute_regression_seconds, control * settings.max_relative_regression)
    return candidate <= control + tolerance


def payback_precheck(plan_result: dict, settings: Settings) -> dict:
    """Cheaply reject obviously poor verification payback when history is deep enough."""

    evidence = plan_result["evidence"]
    measured = int(evidence.get("measured_builds") or 0)
    baseline = evidence.get("median_duration_seconds")
    likelihood = float(plan_result["optimization_signal"]["max_change_likelihood"])
    if measured < 3 or baseline is None or likelihood <= 0:
        return {
            "decision": "insufficient-history",
            "estimated_verification_seconds": None,
            "estimated_break_even_deploys": None,
            "assumption": "Explicit optimization may proceed within the hard time budget.",
        }
    baseline = float(baseline)
    estimated_verification = baseline * (settings.trials + 3)
    estimated_savings = baseline * likelihood * 0.5
    break_even = estimated_verification / estimated_savings if estimated_savings > 0 else None
    return {
        "decision": "run" if break_even is not None and break_even <= settings.payback_deploys else "skip",
        "estimated_verification_seconds": round(estimated_verification, 1),
        "estimated_break_even_deploys": round(break_even, 1) if break_even is not None else None,
        "assumption": "Half of the change-weighted historical median is avoidable; measured proof replaces this estimate.",
    }


def _evaluate(
    control: Sequence[BuildResult], candidate: Sequence[BuildResult],
    no_op: tuple[BuildResult, BuildResult], dependency: tuple[BuildResult, BuildResult] | None,
    correctness: Sequence[bool], settings: Settings, verification_seconds: float,
) -> tuple[dict, dict]:
    control_stats, candidate_stats = _stats(control), _stats(candidate)
    absolute = control_stats["median_seconds"] - candidate_stats["median_seconds"]
    relative = absolute / control_stats["median_seconds"] if control_stats["median_seconds"] else 0.0
    p95_allowed = _regression_allowed(control_stats["p95_seconds"], candidate_stats["p95_seconds"], settings)
    no_op_allowed = _regression_allowed(no_op[0].duration_seconds, no_op[1].duration_seconds, settings)
    dependency_allowed = True
    if dependency:
        dependency_allowed = _regression_allowed(dependency[0].duration_seconds, dependency[1].duration_seconds, settings)
    payback = verification_seconds / absolute if absolute > 0 else None
    gates = {
        "builds_passed": all(value.return_code == 0 for value in [*control, *candidate, *no_op, *(dependency or ())]),
        "verification_contract_present": bool(correctness),
        "verification_commands_passed": bool(correctness) and all(correctness),
        "median_improvement": absolute >= settings.min_absolute_seconds and relative >= settings.min_relative_improvement,
        "p95_not_regressed": p95_allowed,
        "no_op_not_regressed": no_op_allowed,
        "dependency_change_not_regressed": dependency_allowed if dependency else False,
        "payback_within_limit": payback is not None and payback <= settings.payback_deploys,
        "budget_respected": verification_seconds <= settings.budget_seconds,
    }
    benchmark = {
        "source_change": {
            "control": control_stats,
            "candidate": candidate_stats,
            "absolute_improvement_seconds": round(absolute, 3),
            "relative_improvement": round(relative, 4),
        },
        "no_op": {
            "control_seconds": no_op[0].duration_seconds,
            "candidate_seconds": no_op[1].duration_seconds,
        },
        "dependency_change": None if not dependency else {
            "control_seconds": dependency[0].duration_seconds,
            "candidate_seconds": dependency[1].duration_seconds,
        },
        "verification_seconds": round(verification_seconds, 3),
        "estimated_break_even_deploys": round(payback, 1) if payback is not None else None,
    }
    return benchmark, gates


def _build_or_fail(
    build_runner: BuildRunner, root: Path, dockerfile: Path, tag: str,
    settings: Settings, deadline: float,
) -> BuildResult:
    result = build_runner(root, dockerfile, tag, settings, deadline)
    if result.return_code != 0:
        raise VerificationFailure(result.error_kind or "docker-build-failed")
    return result


def verify(
    root: Path, dockerfile: Path, candidate: Candidate, settings: Settings, optimizer,
    build_runner: BuildRunner = _run_docker_build,
    command_runner: CommandRunner = _run_command,
) -> dict:
    started = time.monotonic()
    operation_id = _sha256(f"{time.time_ns()}-{os.getpid()}".encode("utf-8"))[:16]
    deadline = started + settings.budget_seconds
    relative_dockerfile = _relative_dockerfile(root, dockerfile)
    source_path = _safe_source_path(root, settings.source_path, optimizer)
    dependency_path = _safe_manifest_path(root, optimizer)
    if not source_path:
        raise ValueError("no supported representative source file was found; set benchmark.source_path in .dlo.yml")
    if not dependency_path:
        raise ValueError("no dependency manifest was found for the required negative-control build")
    preimages = _preimages(root, candidate.affected_paths)
    slug = re.sub(r"[^a-z0-9_.-]+", "-", root.name.lower()).strip("-._") or "project"
    tag_base = f"dlo-verify/{slug}-{candidate.candidate_id}"
    tags = [f"{tag_base}:control", f"{tag_base}:candidate"]
    try:
        with tempfile.TemporaryDirectory(prefix="dlo-optimize-") as directory:
            temporary = Path(directory)
            control_root, candidate_root = temporary / "control", temporary / "candidate"
            _copy_tree(root, control_root)
            _copy_tree(root, candidate_root)
            _apply_patch(candidate_root, candidate.patch)
            control_dockerfile = control_root / relative_dockerfile
            candidate_dockerfile = candidate_root / relative_dockerfile

            _build_or_fail(build_runner, control_root, control_dockerfile, tags[0], settings, deadline)
            _build_or_fail(build_runner, candidate_root, candidate_dockerfile, tags[1], settings, deadline)

            environment = dict(os.environ)
            environment["DLO_IMAGE_TAG"] = tags[1]
            environment["DLO_PROJECT_ROOT"] = str(candidate_root)
            correctness = [
                command_runner(command, candidate_root, environment, deadline) == 0
                for command in settings.verification_commands
            ]
            if correctness and not all(correctness):
                raise VerificationFailure("correctness-command-failed")

            control_source, candidate_source = control_root / source_path, candidate_root / source_path
            control_runs: list[BuildResult] = []
            candidate_runs: list[BuildResult] = []
            for trial in range(settings.trials):
                marker = f"source-{trial}-{operation_id}"
                _mutate(control_source, marker)
                _mutate(candidate_source, marker)
                order = (
                    ((control_root, control_dockerfile, tags[0], control_runs),
                     (candidate_root, candidate_dockerfile, tags[1], candidate_runs))
                    if trial % 2 == 0 else
                    ((candidate_root, candidate_dockerfile, tags[1], candidate_runs),
                     (control_root, control_dockerfile, tags[0], control_runs))
                )
                for build_root, build_dockerfile, tag, values in order:
                    values.append(_build_or_fail(build_runner, build_root, build_dockerfile, tag, settings, deadline))

            no_op = (
                _build_or_fail(build_runner, control_root, control_dockerfile, tags[0], settings, deadline),
                _build_or_fail(build_runner, candidate_root, candidate_dockerfile, tags[1], settings, deadline),
            )
            _mutate(control_root / dependency_path, f"dependency-{operation_id}", manifest=True)
            _mutate(candidate_root / dependency_path, f"dependency-{operation_id}", manifest=True)
            dependency = (
                _build_or_fail(build_runner, control_root, control_dockerfile, tags[0], settings, deadline),
                _build_or_fail(build_runner, candidate_root, candidate_dockerfile, tags[1], settings, deadline),
            )
            elapsed = time.monotonic() - started
            benchmark, gates = _evaluate(
                control_runs, candidate_runs, no_op, dependency, correctness, settings, elapsed,
            )
    finally:
        try:
            subprocess.run(
                ["docker", "image", "rm", "-f", *tags], stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, check=False,
            )
        except FileNotFoundError:
            pass
    _assert_preimages(root, preimages)
    gates["protected_changes_absent"] = not candidate.protected_changes
    verified = all(gates.values())
    return {
        "operation_id": operation_id,
        "benchmark": benchmark,
        "gates": gates,
        "verified": verified,
        "preimages": preimages,
    }


def _proof_directory(root: Path, optimizer) -> Path:
    return optimizer.state_path(root) / "proofs"


def _prune_proofs(root: Path, optimizer, now: dt.datetime | None = None) -> dict:
    current = now or dt.datetime.now(dt.timezone.utc)
    directory = _proof_directory(root, optimizer)
    if not directory.is_dir():
        return {"deleted": 0, "retained": 0}
    retained: list[tuple[Path, dict, dt.datetime]] = []
    deleted = 0
    for path in directory.glob("*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            timestamp = dt.datetime.fromisoformat(str(value["timestamp"]))
            lifetime = dt.timedelta(days=30 if value.get("verified") else 7)
            if current - timestamp > lifetime:
                path.unlink()
                deleted += 1
            else:
                retained.append((path, value, timestamp))
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            path.unlink(missing_ok=True)
            deleted += 1
    retained.sort(key=lambda item: item[2], reverse=True)
    for path, _value, _timestamp in retained[20:]:
        path.unlink(missing_ok=True)
        deleted += 1
    return {"deleted": deleted, "retained": min(len(retained), 20)}


def _write_proof(root: Path, proof: dict, optimizer) -> Path:
    directory = _proof_directory(root, optimizer)
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    timestamp = str(proof["timestamp"]).replace(":", "-").replace("+", "-")
    path = directory / f"{timestamp}-{proof['candidate_id']}.json"
    path.write_text(json.dumps(proof, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    _prune_proofs(root, optimizer)
    return path


def _sanitize_proof(root: Path, candidate: Candidate, verification: dict, applied: bool) -> dict:
    benchmark = verification["benchmark"]
    return {
        "schema_version": 1,
        "kind": "optimization_proof",
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        "project_root_hash": _sha256(str(root).encode("utf-8")),
        "candidate_id": candidate.candidate_id,
        "operation_id": verification.get("operation_id"),
        "candidate_origin": candidate.origin,
        "candidate_kind": candidate.kind,
        "affected_paths": list(candidate.affected_paths),
        "protected_changes": list(candidate.protected_changes),
        "verified": verification["verified"],
        "applied": applied,
        "benchmark": benchmark,
        "gates": verification["gates"],
        "failure_kind": verification.get("failure_kind"),
        "before_sha256": verification["preimages"],
    }


def _compact_event(root: Path, candidate: Candidate, proof: dict) -> dict:
    return {
        "schema_version": 3,
        "timestamp": proof["timestamp"],
        "kind": "optimize",
        "status": "success" if proof["applied"] else "partial",
        "project_root": str(root),
        "changed_paths": list(candidate.affected_paths) if proof["applied"] else [],
        "tags": ["verified" if proof["verified"] else "unverified", candidate.kind],
        "duration_seconds": proof["benchmark"]["verification_seconds"],
    }


def run(args, optimizer) -> tuple[int, dict]:
    root = optimizer.project_root(args.root)
    dockerfile = optimizer.dockerfile_path(root, args.dockerfile)
    patch = Path(args.candidate).read_text(encoding="utf-8") if args.candidate else None
    result = plan(root, dockerfile, optimizer, patch)
    if args.plan or result["candidate"] is None:
        return 0, result
    candidate = Candidate(**{
        **result["candidate"],
        "affected_paths": tuple(result["candidate"]["affected_paths"]),
        "protected_changes": tuple(result["candidate"]["protected_changes"]),
    })
    if args.apply_approved:
        if args.apply_approved != candidate.candidate_id:
            raise ValueError("--apply-approved must exactly match the planned candidate ID")
        preimages = _preimages(root, candidate.affected_paths)
        _apply_patch(root, candidate.patch, check=True)
        _assert_preimages(root, preimages)
        _apply_patch(root, candidate.patch)
        result.update({
            "kind": "optimization_result", "status": "approved-applied", "applied": True,
            "next_action": "Review the working-tree diff; commit it or use Git to restore it.",
            "candidate": {key: value for key, value in result["candidate"].items() if key != "patch"},
        })
        return 0, result
    settings = settings_from_args(root, args)
    precheck = payback_precheck(result, settings)
    result["payback_precheck"] = precheck
    if precheck["decision"] == "skip" and not args.force:
        result.update({
            "kind": "optimization_result", "status": "skipped-payback", "applied": False,
            "candidate": {key: value for key, value in result["candidate"].items() if key != "patch"},
            "next_action": "Use --force to benchmark despite the estimated payback, or collect more representative history.",
        })
        return 3, result
    verification_started = time.monotonic()
    try:
        verification = verify(root, dockerfile, candidate, settings, optimizer)
    except (VerificationFailure, TimeoutError) as exc:
        failure_kind = exc.kind if isinstance(exc, VerificationFailure) else "budget-exhausted"
        verification = {
            "operation_id": _sha256(f"{time.time_ns()}-{os.getpid()}".encode("utf-8"))[:16],
            "benchmark": {
                "source_change": None, "no_op": None, "dependency_change": None,
                "verification_seconds": round(time.monotonic() - verification_started, 3),
                "estimated_break_even_deploys": None,
            },
            "gates": {"verification_completed": False},
            "verified": False,
            "preimages": _preimages(root, candidate.affected_paths),
            "failure_kind": failure_kind,
        }
    applied = False
    if verification["verified"]:
        _assert_preimages(root, verification["preimages"])
        _apply_patch(root, candidate.patch)
        applied = True
    proof = _sanitize_proof(root, candidate, verification, applied)
    proof_path = _write_proof(root, proof, optimizer)
    optimizer.append_event(root, _compact_event(root, candidate, proof))
    result.update({
        "kind": "optimization_result",
        "status": "verified-applied" if applied else "rejected",
        "applied": applied,
        "verification": {key: value for key, value in verification.items() if key != "preimages"},
        "proof_file": str(proof_path),
        "candidate": {key: value for key, value in result["candidate"].items() if key != "patch"},
        "next_action": (
            "Review the working-tree diff; commit it or use Git to restore it."
            if applied else
            "Inspect the failed proof gates, revise the candidate or verification contract, and plan again."
        ),
    })
    return (0 if applied else 3), result


def render(result: dict) -> str:
    candidate = result.get("candidate")
    if not candidate:
        return "DLO found no conservative built-in optimization candidate. An agent may submit one with --candidate."
    lines = [
        f"DLO optimization {result['status']}: {candidate['candidate_id']}",
        f"Candidate: {candidate['kind']} ({candidate['origin']})",
        f"Affected paths: {', '.join(candidate['affected_paths'])}",
        candidate["rationale"],
    ]
    if result["kind"] == "optimization_plan":
        if candidate["protected_changes"]:
            lines.append("Approval-only changes: " + ", ".join(candidate["protected_changes"]))
        lines.extend(["", candidate["patch"].rstrip(), "", result["next_action"]])
        return "\n".join(lines)
    verification = result.get("verification")
    precheck = result.get("payback_precheck")
    if precheck and precheck["decision"] == "skip":
        lines.append(
            f"Payback precheck skipped verification: estimated break-even "
            f"{precheck['estimated_break_even_deploys']} deploys."
        )
        lines.append(result["next_action"])
        return "\n".join(lines)
    if verification:
        source = verification["benchmark"]["source_change"]
        if source:
            lines.append(
                f"Source-change median: {source['control']['median_seconds']}s -> "
                f"{source['candidate']['median_seconds']}s "
                f"({source['relative_improvement']:.1%}, {source['absolute_improvement_seconds']}s)"
            )
        lines.append(
            f"Verification cost: {verification['benchmark']['verification_seconds']}s; "
            f"break-even: {verification['benchmark']['estimated_break_even_deploys']} deploys"
        )
        failed = [name for name, passed in verification["gates"].items() if not passed]
        lines.append("Proof gates: " + ("all passed" if not failed else "failed " + ", ".join(failed)))
        if verification.get("failure_kind"):
            lines.append(f"Failure: {verification['failure_kind']}")
        lines.append(f"Proof: {result['proof_file']}")
    lines.append("Patch applied to the working tree." if result.get("applied") else "Project left unchanged.")
    return "\n".join(lines)
