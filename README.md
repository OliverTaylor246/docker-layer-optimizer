# Docker Layer Optimizer

`dlo` is a deterministic CLI for finding and verifying Docker cache improvements. It observes which BuildKit steps were cached or rebuilt, compares image layers between successful builds, and learns which project paths change from Git and local build history. The bundled Codex and Claude skill interprets those facts; Wendy is not required.

Wrapping a build does not itself make Docker faster. The speedup comes from Dockerfile and context changes that `dlo` identifies and measures.

## Install

Python 3.9+, Git, Docker, and Docker Buildx are required for measured builds. Static analysis works without Docker.

```sh
python3 -m pip install docker-layer-optimizer==0.4.0b1
```

Until the beta is available from PyPI, install the repository directly:

```sh
python3 -m pip install git+https://github.com/OliverTaylor246/docker-layer-optimizer.git
```

Maintainers: the release workflow publishes tags to GitHub. PyPI publishing remains gated by the `PYPI_TRUSTED_PUBLISHING=true` repository variable (or a deliberate manual run) so a missing first-project Trusted Publisher cannot make an otherwise valid GitHub release fail.

## Measure a build

```sh
dlo build --root /path/to/project --tag my-app:dev
```

For the default `--load` build, the result includes:

- cached, rebuilt, resolved, failed, and incomplete Dockerfile steps from BuildKit's structured progress stream;
- matching and unmatched uncompressed layer DiffIDs, changed ordered-chain positions, and common-prefix length;
- build duration, transferred context size, paths changed since the prior attempt, and observer overhead.

For a pushed image, compare compressed OCI registry blobs instead:

```sh
dlo build --root . --tag registry.example.com/team/app:dev --push
```

`unmatched_compressed_bytes` is the size of current compressed blobs absent from the previous observed manifest. It is a deterministic upper-bound-style comparison, not a measurement of network bytes uploaded; the registry or proxy may already contain blobs.

BuildKit `rawjson` is preferred because it supplies an explicit cache flag. `dlo` falls back to plain progress only when the installed Buildx rejects `rawjson`; choose explicitly with `--progress-format rawjson|plain`.

Pass common build settings directly:

```sh
dlo build --root . --dockerfile docker/Dockerfile --tag my-app:dev \
  --platform linux/amd64 --target runtime --build-arg VERSION=dev
```

## Profile a deployment

Wrap the deployment command after `--` to measure the complete path rather than treating every delay as a Docker build problem:

```sh
dlo deploy --root . --target woof -- wendy --device woof.local run --detach --yes
dlo deploy --root . --target staging -- docker compose up --build -d --wait
```

`dlo deploy` auto-detects Wendy and Docker Compose output and divides observed time into build, export, transfer, unpack, replacement, and readiness phases. It records changed project paths and target-scoped timing history, then `dlo analyze` reports median phases and recommends whether to work on Docker layers or container startup/readiness.

For another deployment system, add output markers without writing an adapter:

```sh
dlo deploy --root . --adapter generic \
  --phase-marker 'build=^Compiling' \
  --phase-marker 'readiness=^Service ready$' \
  -- ./deploy.sh
```

Phase timing is based on when output markers are received. It is deterministic for a given stream but is not internal telemetry from the deployment platform. The wrapped command and output logs are never persisted. Use `--quiet` to hide command output or `--json` for a machine-readable observation.

## Analyze and learn

```sh
dlo analyze --root /path/to/project
dlo analyze --root /path/to/project --json
dlo history --root /path/to/project
```

The analyzer:

- maps `COPY` and `ADD` inputs to tracked context files;
- estimates change likelihood from recency-weighted Git and local observations;
- ranks invalidation points by change likelihood × downstream comparative cost;
- finds broad copies before dependency installation and missing `.dockerignore` files;
- reports measured step, DiffID, registry-blob, context, and overhead evidence separately.

It recommends changes but does not rewrite Dockerfiles automatically. Layer order is constrained by build semantics, so validate every restructuring with representative builds.

Builds and profiled deployments automatically snapshot effective local context paths and hashes. A relevant non-build task can be recorded without storing its description:

```sh
dlo record --root . --kind task --status success --from-git --tag dependencies
```

## Privacy and state

Observations live outside the repository in the operating system's user cache:

- macOS: `~/Library/Caches/docker-layer-optimizer/`
- Linux: `${XDG_CACHE_HOME:-~/.cache}/docker-layer-optimizer/`
- Windows: `%LOCALAPPDATA%/docker-layer-optimizer/`

Set `DLO_CACHE_DIR` to override the base directory. The tool persists paths and their hashes, project and image identifiers, coarse deployment target names and signals, phase timings, tags, timestamps, durations, layer/blob digests, sizes, and counts. It does not persist build or deployment commands, output logs, Dockerfile instruction text, source contents, prompts, secret contents, environment values, or build-argument values. State writes and same-target builds or deployments are locked for concurrent use.

See [SECURITY.md](SECURITY.md) for the threat model and disclosure process and [the observation schema](docs/observation-schema-v3.json) for the machine-readable contract.

## Agent skill

Codex:

```sh
codex plugin marketplace add OliverTaylor246/docker-layer-optimizer
codex plugin add docker-layer-optimizer@docker-optimization-tools
```

Claude Code:

```sh
claude plugin marketplace add OliverTaylor246/docker-layer-optimizer
claude plugin install docker-layer-optimizer@docker-optimization-tools
```

Ask the agent to use `optimize-docker-layers`. The CLI remains the deterministic measurement engine; the skill adds project-aware interpretation and safe edits.

## Compatibility

| Capability | Requirement | Notes |
|---|---|---|
| Static analysis | Python 3.9+, Git | Docker is optional. |
| Structured step counts | Docker Buildx with `--progress=rawjson` | Plain-progress fallback is less robust. |
| Local layer comparison | A successful `--load` exporter | Compares uncompressed DiffIDs. |
| Registry comparison | A readable pushed OCI/Docker manifest | Compares compressed blob digests and declared sizes. |
| Deployment profiling | A command with recognizable or custom output markers | Built-in Wendy and Docker Compose adapters; does not require Docker. |
| Platforms | Linux, macOS, Windows | Unit-tested on all three; real-Docker CI runs on Linux. |
| Builders | Docker and docker-container Buildx drivers | Remote behavior depends on exporter and registry access. |

## Benchmarks and development

Run the reproducible Python, Node, Go, and monorepo matrix with five source edits, five dependency edits, and five no-op overhead measurements per layout:

```sh
python3 -m pip install -e .
python3 benchmarks/run_benchmarks.py --iterations 5 --output benchmark.json
```

The synthetic benchmark compares an intentionally broad `COPY . .` control with a manifest-first Dockerfile. Results report medians and p95 values; they are not universal performance claims. The separate [G1 and Woof development-lifecycle baseline](benchmarks/results/2026-08-01-development-lifecycle.md) treats warm-cache deployment as the normal case and attributes no speedup to DLO unless a concrete optimization is applied. A later [five-run end-to-end Woof benchmark](benchmarks/results/2026-08-02-woof-end-to-end.md) measured a concrete layout change through build, transfer, device unpack, replacement, and readiness. See [the methodology](docs/benchmarking.md) and the [five-run Colima ARM64 result](benchmarks/results/2026-08-01-colima-arm64.md).

```sh
python3 -m unittest discover -s tests -v
python3 tests/docker_integration.py --registry 127.0.0.1:5000
dlo --help
```

## License

MIT
