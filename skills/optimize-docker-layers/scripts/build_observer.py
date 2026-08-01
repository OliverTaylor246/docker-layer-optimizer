"""Deterministic BuildKit observer used by the docker-layer-optimizer CLI."""

from __future__ import annotations

from collections import Counter
import datetime as dt
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from typing import Iterable, Sequence


PROGRESS_LINE = re.compile(r"^#(?P<id>\d+)\s+(?P<body>.*)$")
STEP_START = re.compile(r"^\[(?P<label>[^]]*\d+/\d+)\]\s+(?P<display>.+)$")
DONE = re.compile(r"^DONE(?:\s+(?P<seconds>[0-9.]+)s)?$")
ERROR = re.compile(r"^ERROR(?:(?:\s+(?P<seconds>[0-9.]+)s)|(?::.*))?$")
TRANSFER = re.compile(r"transferring context:\s+(?P<size>[0-9.]+)(?P<unit>[kMGT]?B)", re.IGNORECASE)
SIZE_MULTIPLIERS = {"b": 1, "kb": 1_000, "mb": 1_000_000, "gb": 1_000_000_000, "tb": 1_000_000_000_000}


class BuildProgressParser:
    """Parse stable facts from Docker's documented `--progress=plain` output."""

    def __init__(self) -> None:
        self.vertices: dict[str, dict] = {}
        self.context_bytes: int | None = None

    def feed(self, line: str) -> None:
        match = PROGRESS_LINE.match(line.strip())
        if not match:
            return
        vertex_id, body = match.group("id"), match.group("body")
        transfer = TRANSFER.search(body)
        if transfer:
            size = float(transfer.group("size"))
            multiplier = SIZE_MULTIPLIERS[transfer.group("unit").lower()]
            self.context_bytes = int(size * multiplier)

        start = STEP_START.match(body)
        if start:
            self.vertices[vertex_id] = {
                "id": int(vertex_id),
                "display": start.group("display"),
                "status": "running",
                "duration_seconds": None,
            }
            return
        item = self.vertices.get(vertex_id)
        if not item:
            return
        if body == "CACHED":
            item["status"] = "cached"
            item["duration_seconds"] = 0.0
        elif done := DONE.match(body):
            item["status"] = "rebuilt"
            item["duration_seconds"] = float(done.group("seconds")) if done.group("seconds") else None
        elif error := ERROR.match(body):
            item["status"] = "failed"
            item["duration_seconds"] = float(error.group("seconds")) if error.group("seconds") else None
        elif body == "CANCELED":
            item["status"] = "failed"
            item["duration_seconds"] = None

    def summary(self) -> dict:
        items = sorted(self.vertices.values(), key=lambda item: item["id"])
        return {
            "total": len(items),
            "cached": sum(item["status"] == "cached" for item in items),
            "rebuilt": sum(item["status"] == "rebuilt" for item in items),
            "failed": sum(item["status"] == "failed" for item in items),
            "incomplete": sum(item["status"] == "running" for item in items),
            "items": items,
        }


def compare_layers(current: Sequence[str], previous: Sequence[str] | None) -> dict:
    """Compare immutable DiffID content and ordered chain positions."""

    prior = list(previous or [])
    available = Counter(prior)
    reused = 0
    for digest in current:
        if available[digest] > 0:
            available[digest] -= 1
            reused += 1
    prefix = 0
    for left, right in zip(current, prior):
        if left != right:
            break
        prefix += 1
    unchanged_positions = sum(left == right for left, right in zip(current, prior))
    return {
        "total": len(current),
        "new": len(current) - reused,
        "reused": reused,
        "removed": len(prior) - reused,
        "matching_diff_ids": reused,
        "unmatched_diff_ids": len(current) - reused,
        "changed_positions": max(len(current), len(prior)) - unchanged_positions,
        "common_prefix": prefix,
        "has_baseline": previous is not None,
    }


def dockerignore_path(root: Path, dockerfile: Path | None = None) -> Path:
    specific = Path(f"{dockerfile}.dockerignore") if dockerfile else None
    if specific and specific.is_file():
        return specific
    return root / ".dockerignore"


def dockerignore_patterns(root: Path, dockerfile: Path | None = None) -> list[tuple[bool, str]]:
    path = dockerignore_path(root, dockerfile)
    if not path.is_file():
        return []
    patterns = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        value = raw.strip()
        if not value or value.startswith("#"):
            continue
        negated = value.startswith("!")
        value = value[1:] if negated else value
        value = value.replace("\\", "/").lstrip("/").rstrip("/")
        if value:
            patterns.append((negated, value))
    return patterns


def _matches_pattern(path: str, pattern: str) -> bool:
    if "/" not in pattern:
        return any(fnmatch.fnmatch(part, pattern) for part in path.split("/"))
    return fnmatch.fnmatch(path, pattern) or path.startswith(pattern + "/")


def is_ignored(path: str, patterns: Sequence[tuple[bool, str]]) -> bool:
    ignored = False
    for negated, pattern in patterns:
        if _matches_pattern(path, pattern):
            ignored = not negated
    return ignored


def filter_context_files(root: Path, dockerfile: Path, files: Iterable[str]) -> list[str]:
    patterns = dockerignore_patterns(root, dockerfile)
    unavailable = {".dockerignore"}
    canonical_root = root.resolve()
    for path in (dockerfile, dockerignore_path(root, dockerfile)):
        try:
            unavailable.add(path.resolve().relative_to(canonical_root).as_posix())
        except ValueError:
            pass
    return sorted(path for path in files if path not in unavailable and not is_ignored(path, patterns))


def snapshot_context(root: Path, dockerfile: Path | None = None) -> dict[str, str]:
    """Hash the effective local context without storing file contents."""

    patterns = dockerignore_patterns(root, dockerfile)
    has_negations = any(negated for negated, _ in patterns)
    snapshot: dict[str, str] = {}
    for directory, names, files in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        relative_dir = directory_path.relative_to(root)
        kept_names = []
        for name in names:
            relative = (relative_dir / name).as_posix()
            if name == ".git" or (is_ignored(relative, patterns) and not has_negations):
                continue
            kept_names.append(name)
        names[:] = kept_names
        for name in files:
            path = directory_path / name
            relative = path.relative_to(root).as_posix()
            if is_ignored(relative, patterns):
                continue
            try:
                if path.is_symlink():
                    payload = os.readlink(path).encode("utf-8", errors="surrogateescape")
                    digest = hashlib.sha256(payload).hexdigest()
                else:
                    hasher = hashlib.sha256()
                    with path.open("rb") as handle:
                        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                            hasher.update(chunk)
                    digest = hasher.hexdigest()
            except (OSError, PermissionError):
                continue
            snapshot[relative] = digest
    return snapshot


def changed_paths(current: dict[str, str], previous: dict[str, str] | None) -> list[str]:
    if previous is None:
        return []
    names = set(current) | set(previous)
    return sorted(path for path in names if current.get(path) != previous.get(path))


def _load_snapshot(path: Path) -> dict:
    if not path.is_file():
        return {"schema_version": 1, "targets": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict) and isinstance(value.get("targets"), dict):
            return value
    except (json.JSONDecodeError, OSError):
        pass
    return {"schema_version": 1, "targets": {}}


def _write_snapshot(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="snapshot-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def inspect_image(tag: str) -> dict:
    process = subprocess.run(
        ["docker", "image", "inspect", tag], text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=False,
    )
    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip() or f"docker image inspect failed for {tag}")
    values = json.loads(process.stdout)
    if not values:
        raise RuntimeError(f"docker image inspect returned no image for {tag}")
    value = values[0]
    return {
        "id": value.get("Id"),
        "size_bytes": value.get("Size"),
        "repo_digests": value.get("RepoDigests") or [],
        "layer_diff_ids": ((value.get("RootFS") or {}).get("Layers") or []),
    }


def _safe_slug(value: str) -> str:
    return re.sub(r"[^a-z0-9_.-]+", "-", value.lower()).strip("-._") or "project"


def _relative(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _metadata(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    allowed = ("containerimage.digest", "containerimage.config.digest")
    return {key: value[key] for key in allowed if isinstance(value.get(key), str)}


def build_command(args, root: Path, dockerfile: Path, metadata_file: Path, tag: str) -> list[str]:
    command = ["docker", "buildx", "build", "--progress=plain", "--metadata-file", str(metadata_file)]
    command.extend(["--file", str(dockerfile), "--tag", tag])
    command.append("--push" if args.push else "--load")
    for option, value in (
        ("--platform", args.platform), ("--target", args.target),
        ("--builder", args.builder), ("--network", args.network),
    ):
        if value:
            command.extend([option, value])
    if args.no_cache:
        command.append("--no-cache")
    if args.pull:
        command.append("--pull")
    for value in args.build_arg:
        command.extend(["--build-arg", value])
    for flag, values in (
        ("--cache-from", args.cache_from), ("--cache-to", args.cache_to),
        ("--secret", args.secret), ("--ssh", args.ssh), ("--label", args.label),
        ("--build-context", args.build_context), ("--provenance", args.provenance),
        ("--sbom", args.sbom),
    ):
        for value in values:
            command.extend([flag, value])
    command.append(str(root))
    return command


def run_build(args, optimizer) -> int:
    root = optimizer.project_root(args.root)
    dockerfile = optimizer.dockerfile_path(root, args.dockerfile)
    tag = args.tag or f"dlo/{_safe_slug(root.name)}:latest"
    output = "push" if args.push else "load"
    key_payload = json.dumps({
        "root": str(root), "dockerfile": str(dockerfile), "tag": tag,
        "platform": args.platform, "target": args.target, "output": output,
    }, sort_keys=True)
    target_key = hashlib.sha256(key_payload.encode()).hexdigest()[:24]
    state_dir = optimizer.state_path(root)
    state = _load_snapshot(state_dir / "snapshot.json")
    previous = state["targets"].get(target_key)
    current_snapshot = snapshot_context(root, dockerfile)

    parser = BuildProgressParser()
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="dlo-build-") as directory:
        metadata_path = Path(directory) / "metadata.json"
        command = build_command(args, root, dockerfile, metadata_path, tag)
        try:
            process = subprocess.Popen(
                command, cwd=root, text=True, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, bufsize=1,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("docker was not found; install Docker with Buildx or run `dlo analyze` only") from exc
        assert process.stdout is not None
        for line in process.stdout:
            parser.feed(line)
            if not args.quiet and not args.json:
                print(line, end="")
        return_code = process.wait()
        duration = round(time.monotonic() - started, 3)
        metadata = _metadata(metadata_path)

    image = None
    inspect_error = None
    if return_code == 0 and not args.push:
        try:
            image = inspect_image(tag)
            comparison = compare_layers(
                image["layer_diff_ids"],
                (previous or {}).get("layer_diff_ids") if previous else None,
            )
            image.update(comparison)
        except (RuntimeError, json.JSONDecodeError) as exc:
            inspect_error = str(exc)

    event = {
        "schema_version": 2,
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        "kind": "build",
        "status": "success" if return_code == 0 else "failure",
        "project_root": str(root),
        "target_key": target_key,
        "dockerfile": {"path": _relative(root, dockerfile), "sha256": hashlib.sha256(dockerfile.read_bytes()).hexdigest()},
        "tag": tag,
        "platform": args.platform,
        "target": args.target,
        "builder": args.builder,
        "output": output,
        "duration_seconds": duration,
        "context_bytes": parser.context_bytes,
        "steps": parser.summary(),
        "image": image,
        "image_inspect_error": inspect_error,
        "changed_paths": changed_paths(current_snapshot, (previous or {}).get("context") if previous else None),
        "metadata": metadata,
    }
    optimizer.append_event(root, event)
    target_state = dict(previous or {})
    target_state["context"] = current_snapshot
    target_state["last_observation"] = event["timestamp"]
    if image:
        target_state["layer_diff_ids"] = image["layer_diff_ids"]
        target_state["last_successful_image"] = event["timestamp"]
    state["targets"][target_key] = target_state
    _write_snapshot(state_dir / "snapshot.json", state)

    if args.json:
        print(json.dumps(event, indent=2, sort_keys=True))
    else:
        steps = event["steps"]
        print(
            f"dlo: {steps['cached']} cached, {steps['rebuilt']} rebuilt, {steps['failed']} failed, "
            f"{steps['incomplete']} incomplete Dockerfile steps"
        )
        if image:
            baseline = " vs previous build" if image["has_baseline"] else " (baseline recorded)"
            print(
                f"dlo: {image['unmatched_diff_ids']} unmatched, {image['matching_diff_ids']} matching layer DiffIDs; "
                f"{image['changed_positions']} changed chain positions{baseline}"
            )
        elif args.push and return_code == 0:
            print("dlo: pushed build recorded; resulting layer DiffIDs require a locally loaded image")
        elif inspect_error:
            print(f"dlo: build recorded, but image layers could not be inspected: {inspect_error}", file=sys.stderr)
    return return_code


def render_history(events: Iterable[dict]) -> str:
    rows = []
    for event in events:
        timestamp = str(event.get("timestamp", "?"))[:19].replace("T", " ")
        kind, status = event.get("kind", "?"), event.get("status", "?")
        details = []
        steps = event.get("steps")
        if isinstance(steps, dict):
            details.append(f"steps {steps.get('cached', 0)} cached/{steps.get('rebuilt', 0)} rebuilt")
        image = event.get("image")
        if isinstance(image, dict):
            unmatched = image.get("unmatched_diff_ids", image.get("new", "?"))
            matching = image.get("matching_diff_ids", image.get("reused", "?"))
            details.append(f"DiffIDs {unmatched} unmatched/{matching} matching")
        changed = event.get("changed_paths")
        if isinstance(changed, list):
            details.append(f"{len(changed)} changed paths")
        tags = event.get("tags")
        if isinstance(tags, list) and tags:
            details.append(f"tags {','.join(str(tag) for tag in tags)}")
        if event.get("duration_seconds") is not None:
            details.append(f"{event['duration_seconds']}s")
        rows.append(f"{timestamp}  {kind:<6} {status:<7}  {', '.join(details) or 'no measurements'}")
    return "\n".join(rows) if rows else "No local observations recorded."
