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


def _opcode(display: str) -> str:
    return display.lstrip().split(" ", 1)[0].upper() if display.strip() else "UNKNOWN"


def _duration_seconds(started: str | None, completed: str | None) -> float | None:
    if not started or not completed:
        return None
    try:
        def parse(value: str) -> dt.datetime:
            normalized = value.replace("Z", "+00:00")
            normalized = re.sub(
                r"\.(\d+)(?=[+-])",
                lambda match: "." + match.group(1)[:6].ljust(6, "0"),
                normalized,
            )
            return dt.datetime.fromisoformat(normalized)
        start = parse(started)
        end = parse(completed)
        return round(max(0.0, (end - start).total_seconds()), 3)
    except ValueError:
        return None


def _public_item(item: dict) -> dict:
    display = str(item.get("display", ""))
    return {
        "step": item.get("label"),
        "opcode": _opcode(display),
        "instruction_sha256": hashlib.sha256(display.encode("utf-8")).hexdigest(),
        "status": item.get("status"),
        "duration_seconds": item.get("duration_seconds"),
    }


def _summary(items: Sequence[dict], progress_format: str) -> dict:
    values = list(items)
    return {
        "progress_format": progress_format,
        "total": len(values),
        "cached": sum(item["status"] == "cached" for item in values),
        "rebuilt": sum(item["status"] == "rebuilt" for item in values),
        "resolved": sum(item["status"] == "resolved" for item in values),
        "failed": sum(item["status"] == "failed" for item in values),
        "incomplete": sum(item["status"] == "running" for item in values),
        "items": [_public_item(item) for item in values],
    }


def _render_completion(item: dict) -> str:
    status = item["status"]
    duration = item.get("duration_seconds")
    suffix = f" {duration:.3f}s" if duration is not None else ""
    return f"{status:8} [{item.get('label', '?')}] {item.get('display', '')}{suffix}"


def _step_sort_key(item: dict) -> tuple[int, int, str]:
    label = str(item.get("label", ""))
    match = re.search(r"(\d+)/(\d+)$", label)
    return (int(match.group(2)), int(match.group(1)), label) if match else (0, 0, label)


class RawJsonProgressParser:
    """Parse BuildKit's structured `--progress=rawjson` event stream."""

    progress_format = "rawjson"

    def __init__(self) -> None:
        self.vertices: dict[str, dict] = {}
        self.vertex_names: dict[str, str] = {}
        self.context_bytes: int | None = None
        self.invalid_lines: list[str] = []
        self.failure_messages: list[str] = []
        self.events_seen = 0

    def feed(self, line: str) -> list[str]:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            if line.strip():
                self.invalid_lines.append(line.rstrip()[:1000])
            return []
        if not isinstance(value, dict):
            return []
        self.events_seen += 1
        rendered: list[str] = []
        for vertex in value.get("vertexes", []):
            if not isinstance(vertex, dict):
                continue
            digest = str(vertex.get("digest", ""))
            name = str(vertex.get("name", ""))
            if digest and name:
                self.vertex_names[digest] = name
            start = STEP_START.match(name)
            if not digest or not start:
                continue
            item = self.vertices.setdefault(digest, {
                "label": start.group("label"), "display": start.group("display"),
                "status": "running", "duration_seconds": None,
            })
            previous_status = item["status"]
            item["label"] = start.group("label")
            item["display"] = start.group("display")
            if vertex.get("error"):
                item["status"] = "failed"
                item["duration_seconds"] = _duration_seconds(vertex.get("started"), vertex.get("completed"))
                self.failure_messages.append(str(vertex["error"])[:2000])
            elif vertex.get("completed"):
                if vertex.get("cached") is True:
                    item["status"] = "cached"
                elif _opcode(item["display"]) == "FROM":
                    item["status"] = "resolved"
                else:
                    item["status"] = "rebuilt"
                item["duration_seconds"] = _duration_seconds(vertex.get("started"), vertex.get("completed"))
            elif vertex.get("started"):
                item["status"] = "running"
            if item["status"] not in {"running", previous_status}:
                rendered.append(_render_completion(item))

        for status in value.get("statuses", []):
            if not isinstance(status, dict):
                continue
            vertex_name = self.vertex_names.get(str(status.get("vertex", "")), "")
            if vertex_name == "[internal] load build context" and str(status.get("id", "")).startswith("transferring context:"):
                current = status.get("current")
                if isinstance(current, (int, float)):
                    self.context_bytes = max(self.context_bytes or 0, int(current))
        return rendered

    def summary(self) -> dict:
        items = sorted(self.vertices.values(), key=_step_sort_key)
        return _summary(items, self.progress_format)


class BuildProgressParser:
    """Fallback parser for Docker's `--progress=plain` output."""

    progress_format = "plain"

    def __init__(self) -> None:
        self.vertices: dict[str, dict] = {}
        self.context_bytes: int | None = None
        self.invalid_lines: list[str] = []
        self.failure_messages: list[str] = []
        self.events_seen = 0

    def feed(self, line: str) -> list[str]:
        match = PROGRESS_LINE.match(line.strip())
        if not match:
            if line.strip():
                self.invalid_lines.append(line.rstrip()[:1000])
            return []
        self.events_seen += 1
        vertex_id, body = match.group("id"), match.group("body")
        transfer = TRANSFER.search(body)
        if transfer:
            size = float(transfer.group("size"))
            self.context_bytes = int(size * SIZE_MULTIPLIERS[transfer.group("unit").lower()])
        start = STEP_START.match(body)
        if start:
            self.vertices[vertex_id] = {
                "label": start.group("label"), "display": start.group("display"),
                "status": "running", "duration_seconds": None,
            }
            return []
        item = self.vertices.get(vertex_id)
        if not item:
            return []
        if body == "CACHED":
            item["status"], item["duration_seconds"] = "cached", 0.0
        elif done := DONE.match(body):
            item["status"] = "resolved" if _opcode(item["display"]) == "FROM" else "rebuilt"
            item["duration_seconds"] = float(done.group("seconds")) if done.group("seconds") else None
        elif error := ERROR.match(body):
            item["status"] = "failed"
            item["duration_seconds"] = float(error.group("seconds")) if error.group("seconds") else None
            self.failure_messages.append(body[:2000])
        elif body == "CANCELED":
            item["status"], item["duration_seconds"] = "failed", None
        else:
            return []
        return [_render_completion(item)]

    def summary(self) -> dict:
        items = [self.vertices[key] for key in sorted(self.vertices, key=lambda value: int(value))]
        return _summary(items, self.progress_format)


def compare_layers(current: Sequence[str], previous: Sequence[str] | None) -> dict:
    """Compare immutable DiffID content and ordered chain positions."""

    prior = list(previous or [])
    available = Counter(prior)
    matching = 0
    for digest in current:
        if available[digest] > 0:
            available[digest] -= 1
            matching += 1
    prefix = 0
    for left, right in zip(current, prior):
        if left != right:
            break
        prefix += 1
    unchanged_positions = sum(left == right for left, right in zip(current, prior))
    return {
        "total": len(current), "new": len(current) - matching, "reused": matching,
        "removed": len(prior) - matching, "matching_diff_ids": matching,
        "unmatched_diff_ids": len(current) - matching,
        "changed_positions": max(len(current), len(prior)) - unchanged_positions,
        "common_prefix": prefix, "has_baseline": previous is not None,
    }


def dockerignore_path(root: Path, dockerfile: Path | None = None) -> Path:
    specific = Path(f"{dockerfile}.dockerignore") if dockerfile else None
    return specific if specific and specific.is_file() else root / ".dockerignore"


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
    """Hash the approximate effective local context without retaining contents."""

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
                    digest = hashlib.sha256(os.readlink(path).encode("utf-8", errors="surrogateescape")).hexdigest()
                else:
                    hasher = hashlib.sha256()
                    with path.open("rb") as handle:
                        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                            hasher.update(chunk)
                    digest = hasher.hexdigest()
            except OSError:
                continue
            snapshot[relative] = digest
    return snapshot


def changed_paths(current: dict[str, str], previous: dict[str, str] | None) -> list[str]:
    if previous is None:
        return []
    return sorted(path for path in set(current) | set(previous) if current.get(path) != previous.get(path))


def _load_snapshot(path: Path) -> dict:
    if not path.is_file():
        return {"schema_version": 2, "targets": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict) and isinstance(value.get("targets"), dict):
            return value
    except (json.JSONDecodeError, OSError):
        pass
    return {"schema_version": 2, "targets": {}}


def _write_snapshot(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
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
        "id": value.get("Id"), "size_bytes": value.get("Size"),
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


def build_command(args, root: Path, dockerfile: Path, metadata_file: Path, tag: str, progress_format: str) -> list[str]:
    command = ["docker", "buildx", "build", f"--progress={progress_format}", "--metadata-file", str(metadata_file)]
    command.extend(["--file", str(dockerfile), "--tag", tag, "--push" if args.push else "--load"])
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
        ("--build-context", args.build_context), ("--provenance", args.provenance), ("--sbom", args.sbom),
    ):
        for value in values:
            command.extend([flag, value])
    command.append(str(root))
    return command


def _execute_build(args, root: Path, dockerfile: Path, tag: str, metadata_path: Path, progress_format: str):
    parser = RawJsonProgressParser() if progress_format == "rawjson" else BuildProgressParser()
    command = build_command(args, root, dockerfile, metadata_path, tag, progress_format)
    try:
        process = subprocess.Popen(
            command, cwd=root, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, bufsize=1,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("docker was not found; install Docker with Buildx or run `dlo analyze` only") from exc
    assert process.stdout is not None
    try:
        for line in process.stdout:
            rendered = parser.feed(line)
            if not args.quiet and not args.json:
                for message in rendered:
                    print(message)
    except KeyboardInterrupt:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        raise
    return process.wait(), parser, command


def _rawjson_unsupported(parser: RawJsonProgressParser) -> bool:
    text = "\n".join(parser.invalid_lines).lower()
    return parser.events_seen == 0 and "rawjson" in text and any(word in text for word in ("unknown", "invalid", "unsupported"))


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
    state_dir.mkdir(parents=True, mode=0o700, exist_ok=True)

    with optimizer.file_lock(state_dir / f"target-{target_key}.lock"):
        with optimizer.file_lock(state_dir / "state.lock"):
            state = _load_snapshot(state_dir / "snapshot.json")
            previous = state["targets"].get(target_key)

        wrapper_started = time.monotonic()
        snapshot_started = time.monotonic()
        current_snapshot = snapshot_context(root, dockerfile)
        snapshot_seconds = time.monotonic() - snapshot_started
        build_started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="dlo-build-") as directory:
            metadata_path = Path(directory) / "metadata.json"
            progress_format = "rawjson" if args.progress_format == "auto" else args.progress_format
            return_code, progress, _ = _execute_build(args, root, dockerfile, tag, metadata_path, progress_format)
            if args.progress_format == "auto" and progress_format == "rawjson" and _rawjson_unsupported(progress):
                return_code, progress, _ = _execute_build(args, root, dockerfile, tag, metadata_path, "plain")
            build_seconds = time.monotonic() - build_started
            metadata = _metadata(metadata_path)

        inspection_started = time.monotonic()
        image = registry = None
        inspect_error = None
        inspection_failure = None
        if return_code == 0 and not args.push:
            try:
                image = inspect_image(tag)
                image.update(compare_layers(image["layer_diff_ids"], (previous or {}).get("layer_diff_ids") if previous else None))
            except (RuntimeError, json.JSONDecodeError) as exc:
                inspect_error = str(exc)
                inspection_failure = "local-image-inspection-failed"
        elif return_code == 0 and args.push:
            try:
                from registry_observer import inspect_registry_image
                registry = inspect_registry_image(tag, args.platform, (previous or {}).get("registry_layers") if previous else None)
            except (RuntimeError, ValueError) as exc:
                inspect_error = str(exc)
                inspection_failure = "registry-manifest-inspection-failed"
        inspection_seconds = time.monotonic() - inspection_started

        event = {
            "schema_version": 3,
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
            "kind": "build", "status": "success" if return_code == 0 else "failure",
            "project_root": str(root), "target_key": target_key,
            "dockerfile": {"path": _relative(root, dockerfile), "sha256": hashlib.sha256(dockerfile.read_bytes()).hexdigest()},
            "tag": tag, "platform": args.platform, "target": args.target,
            "builder": args.builder, "output": output,
            "duration_seconds": round(build_seconds, 3),
            "context_bytes": progress.context_bytes, "steps": progress.summary(),
            "image": image, "registry": registry, "inspection_error": inspection_failure,
            "changed_paths": changed_paths(current_snapshot, (previous or {}).get("context") if previous else None),
            "metadata": metadata,
            "overhead": {
                "snapshot_seconds": round(snapshot_seconds, 6),
                "inspection_seconds": round(inspection_seconds, 6),
                "non_build_seconds": round(snapshot_seconds + inspection_seconds, 6),
                "wrapper_seconds": round(time.monotonic() - wrapper_started, 6),
            },
        }

        target_state = dict(previous or {})
        target_state["context"] = current_snapshot
        target_state["last_observation"] = event["timestamp"]
        if image:
            target_state["layer_diff_ids"] = image["layer_diff_ids"]
            target_state["last_successful_image"] = event["timestamp"]
        if registry:
            target_state["registry_layers"] = registry["layers"]
            target_state["last_successful_registry_image"] = event["timestamp"]

        with optimizer.file_lock(state_dir / "state.lock"):
            merged = _load_snapshot(state_dir / "snapshot.json")
            merged["schema_version"] = 2
            merged["targets"][target_key] = target_state
            _write_snapshot(state_dir / "snapshot.json", merged)
            optimizer.append_event_unlocked(root, event)

    if args.json:
        print(json.dumps(event, indent=2, sort_keys=True))
    else:
        steps = event["steps"]
        print(
            f"dlo: {steps['cached']} cached, {steps['rebuilt']} rebuilt, {steps['resolved']} resolved, "
            f"{steps['failed']} failed, {steps['incomplete']} incomplete Dockerfile steps"
        )
        if image:
            baseline = " vs previous build" if image["has_baseline"] else " (baseline recorded)"
            print(
                f"dlo: {image['unmatched_diff_ids']} unmatched, {image['matching_diff_ids']} matching layer DiffIDs; "
                f"{image['changed_positions']} changed chain positions{baseline}"
            )
        if registry:
            baseline = " vs previous push" if registry["has_baseline"] else " (baseline recorded)"
            print(
                f"dlo: {registry['unmatched_blobs']} unmatched/{registry['matching_blobs']} matching compressed blobs; "
                f"{registry['unmatched_compressed_bytes']} unmatched compressed bytes{baseline}"
            )
        if inspect_error:
            print(f"dlo: build recorded, but image inspection failed: {inspect_error}", file=sys.stderr)
        if return_code != 0:
            for message in progress.failure_messages[-3:]:
                print(f"dlo: {message}", file=sys.stderr)
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
            details.append(f"DiffIDs {image.get('unmatched_diff_ids', image.get('new', '?'))} unmatched/{image.get('matching_diff_ids', image.get('reused', '?'))} matching")
        registry = event.get("registry")
        if isinstance(registry, dict):
            details.append(f"blobs {registry.get('unmatched_blobs', '?')} unmatched/{registry.get('matching_blobs', '?')} matching")
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
