# Changelog

## 0.5.0b1 - 2026-08-03

- Add agent-first `dlo optimize` planning for built-in manifest-first and agent-supplied patch candidates.
- Verify candidates in disposable control/candidate snapshots with paired source edits, no-op and dependency negative controls, project commands, median/p95 thresholds, a time budget, and payback gates.
- Auto-apply only fully proven candidates after protected-property and preimage-hash checks; support explicit exact-ID application for approved unverified plans.
- Persist privacy-safe proof records without patches, commands, output, or source contents, with 30-day/20-record verified retention and seven-day failed retention.
- Configure required improvement separately from allowed p95/no-op/dependency-control noise.
- Document the product contract, implementation tickets, primary-source competitive research, agent JSON workflow, and `.dlo.yml` configuration.

## 0.4.0b1 - 2026-08-02

- Add `dlo deploy -- COMMAND` to profile build, export, transfer, unpack, replacement, and readiness phases.
- Auto-detect Wendy and Docker Compose output, with custom `PHASE=REGEX` markers for other deployment systems.
- Persist phase timings, changed paths, coarse failure signals, and target-scoped baselines without storing commands or logs.
- Feed deployment medians and dominant phases into `dlo analyze` recommendations and `dlo history`.
- Detect successful commands with readiness timeouts as partial deployments instead of successful builds.

## 0.3.0b1 - 2026-08-01

- Prefer BuildKit `rawjson` progress and persist privacy-safe step fingerprints instead of instruction text.
- Compare compressed OCI/Docker registry blobs after `--push`, including matching/unmatched bytes and ordered-chain changes.
- Measure context-snapshot and image-inspection overhead separately from Docker build time.
- Lock same-target builds and state writes for concurrent callers.
- Add a four-project benchmark matrix, real-Docker lifecycle tests, and Linux/macOS/Windows unit CI.
- Publish an observation v3 JSON schema and expanded privacy, compatibility, and benchmark documentation.

Registry unmatched bytes describe blobs absent from the previous observed manifest; they are not actual network-transfer bytes.
