# `border-collie-demo` Docker baseline, 2026-08-03

This observation came from ongoing development of
`wendylabsinc/border-collie-demo` on the `codex/live-go2-foundation` branch. It
is the first DLO observation for the repository and is intentionally reported
as a baseline, not as an optimization result.

The read-only `dlo optimize --plan` pass returned `no-candidate`: the repository
had one visible commit and no prior measured builds or deployments, so there
was not enough evidence for a conservative built-in rewrite.

## Local BuildKit measurement

| Measurement | Result |
| --- | ---: |
| DLO version / schema | 0.5.0b1 / build schema 3 |
| Build status | success |
| Dockerfile SHA-256 | `f888e9e06cb28e2bf8e5d278af9cc6cb0a68582e9d682a6299f16e8395b7451a` |
| Build duration | 21.214 s |
| Wrapper duration | 21.240 s |
| Non-build observer overhead | 0.025 s |
| Effective context | 33,791 bytes |
| BuildKit steps | 12 total: 4 cached, 7 rebuilt, 1 resolved |
| Failed / incomplete steps | 0 / 0 |
| Loaded image size | 227,656,068 bytes |
| Image layers | 9 |
| First-baseline unmatched DiffIDs | 9 |

Environment: arm64 macOS, Docker client 29.7.1, Docker server 29.5.2, and
Buildx 0.36.0. The build used BuildKit `rawjson` progress and a local `--load`
output; no image was pushed to an external registry.

Because this was the first observed image, matching/reused layer counts are not
available yet. A later representative source-only edit and a no-op build are
required before DLO can distinguish stable cache behavior from this initial
baseline or recommend a measured change.
