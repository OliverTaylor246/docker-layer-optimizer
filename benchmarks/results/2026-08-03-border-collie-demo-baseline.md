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

## Real development source change

The next observation captured the actual Border Collie preflight implementation
work rather than a synthetic edit. The effective changed paths were three Python
runtime modules and the audience HTML.

| Measurement | Result |
| --- | ---: |
| Build duration | 10.068 s |
| Wrapper duration | 10.096 s |
| Non-build observer overhead | 0.027 s |
| Effective context | 22,680 bytes |
| BuildKit steps | 12 total: 6 cached, 5 rebuilt, 1 resolved |
| Failed / incomplete steps | 0 / 0 |
| Image layers | 9 total: 6 reused, 3 new |
| Common layer prefix | 6 |
| Loaded image size | 227,647,164 bytes |

The expensive pinned system and Python dependency steps were cached. The source
copy/package-install step and the final web copy rebuilt, which matches the
semantic scope of the change. The second build was 11.146 seconds shorter than
the first, but that is a cold-baseline versus warm-source-change observation,
not a controlled optimization claim.

After two measured builds, `dlo analyze` reported a 15.641-second median build,
6.0 median rebuilt steps, 28,235-byte median context, and 0.026-second median
non-build observer overhead. It still found no obvious safe static layer split.
Representative no-op and further source/dependency observations remain necessary
before proposing or proving a Dockerfile change.

## Controlled candidate proof

A later agent-authored candidate moved runtime source out of the dependency
installation layer and into its own final-image copy. DLO warmed both layouts,
ran three paired source-only trials, a no-op control, a dependency-manifest
negative control, and the repository's correctness contract before applying the
candidate.

| Measurement | Control | Candidate | Result |
| --- | ---: | ---: | ---: |
| Source-edit median, 3 trials | 8.743 s | 0.598 s | 8.145 s / 93.16% faster |
| Source-edit p95 | 8.967 s | 0.604 s | no regression |
| Median cached steps | 6 | 8 | +2 |
| Median rebuilt steps | 5 | 2 | -3 |
| Warm no-op | 0.502 s | 0.497 s | no regression |
| Dependency edit | 22.169 s | 8.141 s | no regression |

Verification took 91.301 seconds and produced an estimated 11.2-deployment
break-even. All configured gates passed, including correctness, protected-path
safety, median and absolute improvement, p95, no-op, dependency control, budget,
and payback. DLO then applied the candidate. The applied Dockerfile digest is
`bfe0300a506aef952bea9a6e3c4006fb8bd8d20ff008716793db0604d1ac2aa2`.

Three subsequent unchanged builds measured the observer separately. Median
Docker duration was 0.547 seconds, median wrapper duration was 0.569921 seconds,
and median non-build observer overhead was 0.021603 seconds. The observer cost
was about 3.95% of the short no-op build and 0.27% of the verified per-source
edit saving.

The applied local image was 227,625,311 bytes with 10 layers. Unchanged builds
reused all 10 layers and rebuilt zero steps. The controlled proof reports
changed layer identities/counts and rebuilt steps, but local `--load` output did
not provide changed layer byte totals per paired trial. A registry-backed run is
still needed to compare unmatched compressed bytes and to split a real Woof
deployment into build, transfer, replacement, and readiness phases.

## First normal post-proof observation

The next production-style build combined a Python Home capture implementation
with a README update. It took 23.949 seconds, with 4 cached, 6 rebuilt, and 1
resolved BuildKit step. DLO's non-build observer overhead was 0.023543 seconds.
The 227,626,218-byte image had 10 layers, reusing 6 and introducing 4.

The README was still copied beside the dependency manifest in the builder, so
the documentation edit invalidated dependency installation despite the verified
source split. Analysis ranked that instruction at the highest expected rebuild
cost. An agent candidate to remove README was not applied because version
0.5.0b1 rejects Markdown as a representative paired-edit source. Supporting a
safe Markdown mutation target is therefore a concrete optimizer gap exposed by
production use; the repository retained its last fully proved Dockerfile.
