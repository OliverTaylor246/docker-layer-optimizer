"""Inspect pushed OCI/Docker manifests and compare compressed registry blobs."""

from __future__ import annotations

from collections import Counter
import hashlib
import json
import subprocess
from typing import Sequence


ATTESTATION_TYPE = "vnd.docker.reference.type"


def _run_imagetools(reference: str) -> tuple[dict, bytes]:
    process = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", "--raw", reference],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if process.returncode != 0:
        message = process.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(message or f"could not inspect pushed image {reference}")
    raw = process.stdout.rstrip(b"\r\n")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"registry returned invalid manifest JSON for {reference}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"registry returned an unexpected manifest for {reference}")
    return value, raw


def _platform_parts(value: str | None) -> tuple[str, str, str | None] | None:
    if not value:
        return None
    parts = value.split("/")
    if len(parts) < 2:
        raise ValueError(f"platform must be os/architecture[/variant], got {value!r}")
    return parts[0], parts[1], parts[2] if len(parts) > 2 else None


def _is_attestation(descriptor: dict) -> bool:
    annotations = descriptor.get("annotations") or {}
    platform = descriptor.get("platform") or {}
    return ATTESTATION_TYPE in annotations or platform.get("os") == "unknown"


def select_platform_descriptor(index: dict, platform: str | None) -> dict:
    candidates = [item for item in index.get("manifests", []) if isinstance(item, dict) and not _is_attestation(item)]
    requested = _platform_parts(platform)
    if requested:
        os_name, architecture, variant = requested
        candidates = [
            item for item in candidates
            if (item.get("platform") or {}).get("os") == os_name
            and (item.get("platform") or {}).get("architecture") == architecture
            and (variant is None or (item.get("platform") or {}).get("variant") == variant)
        ]
    if len(candidates) != 1:
        detail = f" for platform {platform}" if platform else ""
        raise RuntimeError(f"expected one runnable image manifest{detail}, found {len(candidates)}")
    return candidates[0]


def _digest_reference(reference: str, digest: str) -> str:
    base = reference.split("@", 1)[0]
    if base.rfind(":") > base.rfind("/"):
        base = base[:base.rfind(":")]
    return base + "@" + digest


def compare_registry_layers(current: Sequence[dict], previous: Sequence[dict] | None) -> dict:
    prior = list(previous or [])
    available = Counter(str(item.get("digest")) for item in prior)
    size_by_digest = {str(item.get("digest")): int(item.get("size") or 0) for item in current}
    matching = 0
    matching_bytes = 0
    for item in current:
        digest = str(item.get("digest"))
        if available[digest] > 0:
            available[digest] -= 1
            matching += 1
            matching_bytes += size_by_digest[digest]
    unchanged_positions = sum(
        left.get("digest") == right.get("digest") for left, right in zip(current, prior)
    )
    prefix = 0
    for left, right in zip(current, prior):
        if left.get("digest") != right.get("digest"):
            break
        prefix += 1
    total_bytes = sum(int(item.get("size") or 0) for item in current)
    return {
        "total_layers": len(current),
        "matching_blobs": matching,
        "unmatched_blobs": len(current) - matching,
        "removed_blobs": len(prior) - matching,
        "changed_positions": max(len(current), len(prior)) - unchanged_positions,
        "common_prefix": prefix,
        "total_compressed_bytes": total_bytes,
        "matching_compressed_bytes": matching_bytes,
        "unmatched_compressed_bytes": total_bytes - matching_bytes,
        "has_baseline": previous is not None,
    }


def inspect_registry_image(reference: str, platform: str | None, previous_layers: Sequence[dict] | None = None) -> dict:
    manifest, raw = _run_imagetools(reference)
    index_digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    selected_platform = platform
    if isinstance(manifest.get("manifests"), list):
        descriptor = select_platform_descriptor(manifest, platform)
        digest = str(descriptor["digest"])
        manifest, raw = _run_imagetools(_digest_reference(reference, digest))
        manifest_digest = digest
        selected = descriptor.get("platform") or {}
        selected_platform = "/".join(
            str(value) for value in (selected.get("os"), selected.get("architecture"), selected.get("variant")) if value
        ) or platform
    else:
        manifest_digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    layers = [
        {"digest": str(item.get("digest")), "size": int(item.get("size") or 0), "media_type": item.get("mediaType")}
        for item in manifest.get("layers", []) if isinstance(item, dict) and item.get("digest")
    ]
    if not layers:
        raise RuntimeError(f"no filesystem layers found in pushed image manifest for {reference}")
    result = {
        "reference": reference,
        "index_digest": index_digest if index_digest != manifest_digest else None,
        "manifest_digest": manifest_digest,
        "media_type": manifest.get("mediaType"),
        "platform": selected_platform,
        "config": manifest.get("config"),
        "layers": layers,
    }
    result.update(compare_registry_layers(layers, previous_layers))
    return result
