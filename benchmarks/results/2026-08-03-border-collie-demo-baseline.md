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

A second production-style source-and-documentation build confirmed the finding:
22.009 seconds, 4 cached and 6 rebuilt steps, 6 of 10 image layers reused, a
227,627,267-byte image, and 0.025463 seconds of non-build observer overhead.
Repeated evidence now shows this is a representative workflow cost rather than
a one-off cold-cache result.

## README invalidation proof

Version 0.5.0b2 added a semantics-safe Markdown mutation and moved disposable
verification snapshots from macOS temporary storage into DLO's user cache. The
snapshot change is necessary with Colima because containerized correctness
commands cannot bind-mount `/private/tmp`; snapshots remain ephemeral and are
deleted when verification ends.

The agent candidate removed README from the dependency input while retaining
`pyproject.toml`. DLO verified and automatically applied candidate
`f53c57a25746d2fb56a2`:

| Scenario | Control | Candidate | Change |
| --- | ---: | ---: | ---: |
| README-edit median (3 paired trials) | 19.721 s | 0.391 s | 19.330 s / 98.02% faster |
| README-edit p95 | 20.248 s | 0.431 s | improved |
| Median rebuilt steps | 6 | 0 | -6 |
| No-op | 0.396 s | 0.400 s | within tolerance |
| Dependency-manifest edit | 19.726 s | 20.376 s | within tolerance |

The proof took 109.366 seconds and estimated break-even at 5.7 representative
README-edit deployments. All project correctness, runtime, performance,
negative-control, payback, budget, and protected-change gates passed. This
result applies specifically to this project's real documentation-plus-source
workflow; it is not a universal Docker speedup claim.

## Local registry would-push gate

Before publishing the larger application change, DLO 0.5.0b1 (schema 3) pushed
the pre-DLO control and current verified layout to separate tags in a disposable
localhost registry. No image reached an external registry or device. A second
push added the same deterministic one-file source probe to both layouts, giving
a compressed-blob comparison against each preceding manifest.

| Measurement | Pre-DLO control | Verified DLO layout |
| --- | ---: | ---: |
| Dockerfile SHA-256 | `82768b6182a40481c634507a4c901551e99e3aee2e471b61726675e703cfb023` | `a9c9d59ea2cfd3ec614d32bac8907ef6cfe2473cc53be5744c6026f1b5622b70` |
| First-push effective context | 28,902 B | 1,433 B |
| First-push build duration | 7.210 s | 0.935 s |
| First-push steps | 6 cached / 5 rebuilt / 1 resolved | 8 cached / 2 rebuilt / 1 resolved |
| Full compressed layers | 227,665,785 B across 9 | 227,618,821 B across 10 |
| Source-edit effective context | 8,544 B | 1,497 B |
| Source-edit build duration | 7.070 s | 0.446 s |
| Source-edit matching/unmatched layers | 6 / 3 | 8 / 2 |
| Source-edit unmatched compressed bytes | 99,567,096 B | 21,657 B |
| Source-edit observer overhead | 0.107408 s | 0.109579 s |

The verified layout reduced the source-edit upper-bound transfer by 99,545,439
bytes (99.978%) and the local build/push duration by 6.624 seconds (93.69%). The
control embedded source in its roughly 99.6 MB compressed environment layer;
the candidate kept that layer stable and changed only its small runtime tail.

Uncertainty remains: unmatched manifest bytes are deterministic declared blob
sizes, not packet-level upload telemetry, and a production registry may already
contain shared blobs. Device transfer, unpack, replacement, and readiness have
not yet been measured for this release. Both image layouts passed an import
smoke check, and the source repository passed all 45 project tests.

## Partial Woof deployment comparison

DLO then wrapped real Wendy deployments to the ARM64 Woof device with the
Docker builder and forced content-defined chunking. Hardware motion remained
disabled. Alternating layouts produced a misleading near-tie because each run
displaced the immediately preceding cache path, so those observations are
retained but excluded from proof.

A corrected warmed, consecutive control block completed three source edits in
70.347, 69.641, and 68.682 seconds (69.641-second median). Median phases were
42.697 seconds build, 18.892 export, 0.012 transfer marker, 5.852 unpack, 0.671
replacement, and 1.165 readiness.

One consecutive candidate source edit completed in 11.117 seconds: 8.879 build,
0.164 export, 0.025 transfer marker, 0.045 unpack, 0.422 replacement, and 1.166
readiness. That is 58.524 seconds (84.03%) below the control median, but the
user stopped the matrix before the remaining candidate trials. It is therefore
a preliminary observation, not a verified device-level speedup.

The temporary source probe was removed, the verified candidate was restored,
and the device passed TCP/API readiness. All 45 project tests also passed. The
remaining uncertainty is the missing repeated candidate block; the completed
control block need not be repeated if the same device and deployment path are
still representative when testing resumes.

## Wendy layer-diff release gate

Version 0.5.0b3 made Wendy profiling fail closed on the layer-aware path. Unless
the caller explicitly selects another mode, `dlo deploy` adds
`--chunking force`, enables Wendy's internal timing output, and combines those
reported durations with OCI build, layer-diff, replacement, and readiness
markers.

The release gate used the current verified Dockerfile, a disposable Python
source file, the same ARM64 Woof target, and the same motion-disabled runtime
configuration. DLO was not given a chunking flag; it selected the layer-diff
path itself.

| Phase | Source-file addition | Clean-tree follow-up |
| --- | ---: | ---: |
| End to end | 8.959 s | 6.848 s |
| Build and OCI export | 3.514 s | 1.322 s |
| Layer chunk/query/write | 0.147 s | 0.122 s |
| Replacement | 0.426 s | 0.420 s |
| Readiness | 1.185 s | 1.177 s |
| Reused image layers | 8/10 | 8/10 |

Both deployments reached TCP readiness as `border-collie-demo_app`. A live
container environment check confirmed hardware, autonomy, lab motion, and
perception remained disabled. The disposable probe was removed immediately;
the second observation redeployed the normal production tree.
