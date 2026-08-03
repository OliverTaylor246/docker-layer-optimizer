"""Privacy-safe deployment phase profiler for docker-layer-optimizer."""

from __future__ import annotations

from collections import Counter
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Iterable, Sequence


PHASES = ("build", "export", "transfer", "unpack", "replacement", "readiness")

WENDY_PATTERNS = {
    "build": (
        r"^Building service\b",
        r"^Building and pushing image\b",
        r"^Building image \(OCI layout\)\b",
        r"^\[apple-container\] starting build:",
    ),
    "export": (
        r"exporting to oci image format",
        r"exporting layers",
        r"sending tarball",
    ),
    "transfer": (
        r"^\[apple-container\] pushing image:",
        r"^Pushing image\b",
        r"^Pulling image on device",
        r"^Diffing \d+ layer\(s\) against device",
        r"^Reusing \d+ layer\(s\) already on device",
    ),
    "unpack": (
        r"^Unpack plan:",
        r"^Layer \d+/\d+ (?:reused|applying|unpacked)",
    ),
    "replacement": (
        r"^Creating container for service",
        r"^Creating container\.\.\.",
        r"^Service .+ container created\.",
        r"^App group .+ (?:running|created)",
        r"^Application .+ running in detached mode\.",
    ),
    "readiness": (
        r"^Waiting for .+ to be ready",
        r"^Ready\.$",
        r"readiness probe timed out",
        r"^App reachable at ",
    ),
}

WENDY_TIMING_RE = re.compile(
    r"^\[timing\]\s+(?P<label>.+?)\s+(?P<value>\d+(?:\.\d+)?)(?P<unit>ms|us|µs|s)$",
    re.IGNORECASE,
)

COMPOSE_PATTERNS = {
    "build": (
        r"^\[\+\] Building\b",
        r"^#\d+\b",
        r"^\s*=> \[",
        r"^\s*Building\b",
    ),
    "export": (
        r"exporting to image",
        r"exporting layers",
        r"naming to ",
    ),
    "transfer": (
        r"\b(?:Pushing|Pulling|Downloading|Downloaded)\b",
    ),
    "unpack": (
        r"\b(?:Extracting|Extracted)\b",
    ),
    "replacement": (
        r"\bContainer .+ (?:Creating|Created|Recreate|Recreated|Starting|Started|Stopping|Stopped)\b",
    ),
    "readiness": (
        r"\bContainer .+ (?:Waiting|Healthy|healthy)\b",
        r"\bWaiting for .+ (?:healthy|ready)\b",
    ),
}

SIGNAL_PATTERNS = {
    "readiness-timeout": r"readiness (?:probe )?timed out|timed out .*waiting for .+ready",
    "build-failed": (
        r"\b(?:build|builder) (?:failed|failure)\b|"
        r"\b(?:build|builder) error(?:ed)?(?:\s*:|\s*$)|\bfailed to (?:build|solve)\b"
    ),
    "deployment-failed": (
        r"\b(?:deployment|deploy|container|service) (?:failed|failure)\b|"
        r"\b(?:deployment|deploy|container|service) error(?:ed)?(?:\s*:|\s*$)|"
        r"\bfailed to (?:deploy|create|start|replace)\b"
    ),
}


def infer_adapter(command: Sequence[str]) -> str:
    if not command:
        return "generic"
    executable = Path(command[0]).name.lower()
    lowered = [part.lower() for part in command[:3]]
    if "wendy" in executable:
        return "wendy"
    if executable.startswith("docker-compose") or (executable in {"docker", "docker.exe"} and "compose" in lowered[1:]):
        return "compose"
    return "generic"


def prepare_deploy_command(command: Sequence[str], adapter: str) -> list[str]:
    """Require Wendy's no-fallback layer-diff path unless the user chose a mode."""
    prepared = list(command)
    if adapter != "wendy":
        return prepared
    try:
        run_index = next(index for index, part in enumerate(prepared[1:], start=1) if part.lower() == "run")
    except StopIteration:
        return prepared
    if any(part == "--chunking" or part.startswith("--chunking=") for part in prepared[1:]):
        return prepared
    prepared[run_index + 1:run_index + 1] = ["--chunking", "force"]
    return prepared


def parse_phase_markers(values: Iterable[str]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for value in values:
        phase, separator, pattern = value.partition("=")
        if not separator or phase not in PHASES or not pattern:
            raise ValueError(f"phase marker must be PHASE=REGEX where PHASE is one of {', '.join(PHASES)}")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"invalid phase marker regex for {phase}: {exc}") from exc
        result.setdefault(phase, []).append(pattern)
    return result


def _patterns(adapter: str, custom: dict[str, list[str]] | None = None) -> list[tuple[str, re.Pattern[str]]]:
    source = WENDY_PATTERNS if adapter == "wendy" else COMPOSE_PATTERNS if adapter == "compose" else {}
    combined = {phase: list(source.get(phase, ())) for phase in PHASES}
    for phase, patterns in (custom or {}).items():
        combined[phase].extend(patterns)
    # More terminal/specific phases win if a line could describe two phases.
    priority = ("readiness", "replacement", "unpack", "transfer", "export", "build")
    return [(phase, re.compile(pattern, re.IGNORECASE)) for phase in priority for pattern in combined[phase]]


class DeploymentPhaseTracker:
    """Attribute intervals between known output markers to deployment phases."""

    def __init__(self, adapter: str, custom_markers: dict[str, list[str]] | None = None) -> None:
        self.adapter = adapter
        self.patterns = _patterns(adapter, custom_markers)
        self.started_at: float | None = None
        self.current_phase: str | None = None
        self.current_started: float | None = None
        self.durations = {phase: 0.0 for phase in PHASES}
        self.segments = Counter()
        self.signals: set[str] = set()
        self.reported_timings: dict[str, float] = {}

    def start(self, now: float) -> None:
        self.started_at = now

    def _phase_for(self, line: str) -> str | None:
        for phase, pattern in self.patterns:
            if pattern.search(line):
                return phase
        return None

    def feed(self, line: str, now: float) -> str | None:
        if self.adapter == "wendy":
            match = WENDY_TIMING_RE.search(line)
            if match:
                value = float(match.group("value"))
                unit = match.group("unit").lower()
                if unit == "ms":
                    value /= 1_000
                elif unit in {"us", "µs"}:
                    value /= 1_000_000
                label = match.group("label").strip().lower()
                if label == "build (oci export)":
                    self.reported_timings["build"] = value
                elif label == "chunk+query+write":
                    self.reported_timings["transfer"] = value
                elif label.startswith("runcontainer (assemble+create+start"):
                    self.reported_timings["runcontainer"] = value
        lowered = line.lower()
        for code, pattern in SIGNAL_PATTERNS.items():
            if re.search(pattern, lowered):
                self.signals.add(code)
        phase = self._phase_for(line)
        if phase is None or phase == self.current_phase:
            return phase
        if self.current_phase is not None and self.current_started is not None:
            self.durations[self.current_phase] += max(0.0, now - self.current_started)
        self.current_phase = phase
        self.current_started = now
        self.segments[phase] += 1
        return phase

    def finish(self, now: float) -> dict:
        if self.current_phase is not None and self.current_started is not None:
            self.durations[self.current_phase] += max(0.0, now - self.current_started)
            self.current_started = now
        total = max(0.0, now - (self.started_at if self.started_at is not None else now))
        phases = {
            phase: {
                "duration_seconds": round(self.durations[phase], 3),
                "segments": int(self.segments[phase]),
                "observed": bool(self.segments[phase]),
            }
            for phase in PHASES
        }
        phase_source = "output-markers"
        if self.reported_timings:
            phase_source = "wendy-timing+output-markers"
            for phase in ("build", "transfer"):
                if phase in self.reported_timings:
                    phases[phase]["duration_seconds"] = round(self.reported_timings[phase], 3)
                    phases[phase]["segments"] = max(1, phases[phase]["segments"])
                    phases[phase]["observed"] = True
            if "runcontainer" in self.reported_timings:
                readiness = phases["readiness"]["duration_seconds"] if phases["readiness"]["observed"] else 0.0
                phases["replacement"]["duration_seconds"] = round(
                    max(0.0, self.reported_timings["runcontainer"] - readiness), 3
                )
                phases["replacement"]["segments"] = max(1, phases["replacement"]["segments"])
                phases["replacement"]["observed"] = True
        classified = sum(item["duration_seconds"] for item in phases.values())
        observed = {phase: item["duration_seconds"] for phase, item in phases.items() if item["observed"]}
        dominant = max(observed, key=observed.get) if observed else None
        return {
            "adapter": self.adapter,
            "phase_source": phase_source,
            "phases": phases,
            "dominant_phase": dominant,
            "classified_seconds": round(min(total, classified), 3),
            "unclassified_seconds": round(max(0.0, total - classified), 3),
            "signals": sorted(self.signals),
        }


def _target_key(root: Path, adapter: str, target: str | None) -> str:
    payload = json.dumps({"root": str(root), "adapter": adapter, "target": target or "default"}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _execute(command: Sequence[str], root: Path, tracker: DeploymentPhaseTracker, quiet: bool, json_output: bool):
    environment = None
    if tracker.adapter == "wendy":
        environment = dict(os.environ)
        environment.setdefault("WENDY_TIMING", "1")
    try:
        process = subprocess.Popen(
            list(command), cwd=root, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, bufsize=1, errors="replace", env=environment,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"deployment executable was not found: {command[0]}") from exc
    assert process.stdout is not None
    try:
        for line in process.stdout:
            tracker.feed(line.rstrip("\r\n"), time.monotonic())
            if not quiet and not json_output:
                print(line, end="", flush=True)
    except KeyboardInterrupt:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        raise
    return process.wait()


def render_deployment(event: dict) -> str:
    deployment = event["deployment"]
    rows = [
        f"dlo: deployment {event['status']} in {event['duration_seconds']:.3f}s "
        f"({deployment['adapter']} markers)",
    ]
    for phase in PHASES:
        item = deployment["phases"][phase]
        if item["observed"]:
            rows.append(f"dlo: {phase:<11} {item['duration_seconds']:.3f}s across {item['segments']} segment(s)")
    if deployment["unclassified_seconds"]:
        rows.append(f"dlo: unclassified {deployment['unclassified_seconds']:.3f}s")
    if deployment["dominant_phase"]:
        rows.append(f"dlo: dominant phase: {deployment['dominant_phase']}")
    if deployment["signals"]:
        rows.append(f"dlo: signals: {', '.join(deployment['signals'])}")
    return "\n".join(rows)


def run_deploy(args, optimizer) -> int:
    root = optimizer.project_root(args.root)
    command = list(args.deploy_command or [])
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise ValueError("pass a deployment command after `--`")
    adapter = infer_adapter(command) if args.adapter == "auto" else args.adapter
    command = prepare_deploy_command(command, adapter)
    custom_markers = parse_phase_markers(args.phase_marker or [])
    tracker = DeploymentPhaseTracker(adapter, custom_markers)
    from build_observer import changed_paths, snapshot_context, _load_snapshot, _write_snapshot

    dockerfile = None
    try:
        dockerfile = optimizer.dockerfile_path(root, args.dockerfile)
    except FileNotFoundError:
        pass
    cleaned_targets = optimizer.clean_tags([args.target] if args.target else [])
    target_name = cleaned_targets[0] if cleaned_targets else None
    target_key = _target_key(root, adapter, target_name)
    state_dir = optimizer.state_path(root)
    state_dir.mkdir(parents=True, mode=0o700, exist_ok=True)

    with optimizer.file_lock(state_dir / f"deploy-{target_key}.lock"):
        with optimizer.file_lock(state_dir / "state.lock"):
            state = _load_snapshot(state_dir / "deploy-snapshot.json")
            previous = state["targets"].get(target_key)

        snapshot = snapshot_context(root, dockerfile)
        started = time.monotonic()
        tracker.start(started)
        return_code = _execute(command, root, tracker, args.quiet, args.json)
        completed = time.monotonic()
        deployment = tracker.finish(completed)
        deployment["exit_code"] = return_code
        status = "failure" if return_code else "partial" if deployment["signals"] else "success"
        commit = optimizer.git(root, "rev-parse", "HEAD", check=False).strip() or None
        event = {
            "schema_version": 3,
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
            "kind": "deploy",
            "status": status,
            "project_root": str(root),
            "target_key": target_key,
            "target_name": target_name,
            "commit": commit,
            "dockerfile": {
                "path": dockerfile.relative_to(root).as_posix() if dockerfile and dockerfile.is_relative_to(root) else str(dockerfile),
                "sha256": hashlib.sha256(dockerfile.read_bytes()).hexdigest(),
            } if dockerfile else None,
            "changed_paths": changed_paths(snapshot, (previous or {}).get("context") if previous else None),
            "tags": optimizer.clean_tags(args.tag or []),
            "duration_seconds": round(completed - started, 3),
            "deployment": deployment,
        }

        target_state = dict(previous or {})
        target_state["context"] = snapshot
        target_state["last_observation"] = event["timestamp"]
        with optimizer.file_lock(state_dir / "state.lock"):
            merged = _load_snapshot(state_dir / "deploy-snapshot.json")
            merged["schema_version"] = 2
            merged["targets"][target_key] = target_state
            _write_snapshot(state_dir / "deploy-snapshot.json", merged)
            optimizer.append_event_unlocked(root, event)

    print(json.dumps(event, indent=2, sort_keys=True) if args.json else render_deployment(event))
    return return_code
