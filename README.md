# Docker Layer Optimizer

`dlo` is a deterministic Docker deployment optimizer built for agents and long-lived projects. It learns how a repository changes, identifies expensive cache-invalidating Dockerfile layers, generates safer layouts, and automatically applies a candidate only after paired benchmarks and project-specific correctness checks prove it better.

The engine is a native Go binary. It parses Dockerfiles with the canonical [Moby BuildKit Dockerfile parser](https://github.com/moby/buildkit/tree/master/frontend/dockerfile/parser) and typed instruction decoder, rather than maintaining a second Dockerfile grammar. Wendy is supported but not required.

Wrapping a build does not itself make Docker faster. The speedup comes from a concrete Dockerfile or context change that DLO identifies, proves, and applies.

## Install

Download a static binary from [GitHub Releases](https://github.com/wendylabsinc/docker-layer-optimizer/releases), or install from source with Go 1.26.3+:

```sh
go install github.com/wendylabsinc/docker-layer-optimizer/cmd/dlo@latest
dlo --version
```

Static analysis requires Git. Measured builds and optimization proofs also require Docker with Buildx.

## Optimize with an agent

Planning is read-only and returns evidence, a stable candidate ID, risks, and a unified diff:

```sh
dlo optimize --root . --plan --json
```

Configure the representative edit and correctness contract once:

```yaml
# .dlo.yml
version: 1
verification:
  commands:
    - go test ./...
benchmark:
  source_path: internal/app.go
  trials: 3
  budget_seconds: 600
  min_relative_improvement: 0.10
  min_absolute_seconds: 0.5
  max_relative_regression: 0.10
  max_absolute_regression_seconds: 0.5
  payback_deploys: 20
```

Then benchmark and, if every proof gate passes, apply the built-in candidate:

```sh
dlo optimize --root . --json
```

DLO creates disposable control and candidate snapshots, warms both builds, performs paired source edits, checks no-op and dependency-edit regressions, runs the configured commands, and estimates payback. The real working tree is changed only when every gate passes and the affected files still match their pre-verification hashes.

Agents can submit any Docker-related unified diff through the same proof engine:

```sh
dlo optimize --root . --candidate /tmp/candidate.patch --plan --json
dlo optimize --root . --candidate /tmp/candidate.patch --json
```

Base images, dependency versions, entrypoints, users, ports, health checks, privileges, architecture, and other protected behavior are never auto-applied. An explicitly reviewed plan can be applied without a performance proof only by its exact ID using `--apply-approved ID`.

## Measure a build

```sh
dlo build --root /path/to/project --tag my-app:dev
```

The result separates cached and rebuilt BuildKit steps, context transfer, image layer DiffID changes, build duration, and DLO observer overhead. BuildKit `rawjson` is preferred because it carries an explicit cache flag; plain progress is available as a fallback.

For a pushed image, compare compressed OCI registry blobs:

```sh
dlo build --root . --tag registry.example.com/team/app:dev --push
```

`unmatched_compressed_bytes` is the size of current blobs absent from the previous observed manifest, not a claim about actual network bytes or registry billing.

Common Buildx settings pass through directly:

```sh
dlo build --root . --dockerfile docker/Dockerfile --tag my-app:dev \
  --platform linux/amd64 --target runtime --build-arg VERSION=dev
```

## Profile a deployment

Wrap the actual deployment command so build, transfer, replacement, and readiness are measured separately:

```sh
dlo deploy --root . --target woof -- wendy --device woof.local run --detach --yes
dlo deploy --root . --target staging -- docker compose up --build -d --wait
```

For `wendy run`, DLO enables Wendy timing output and inserts `--chunking force` unless a chunking mode was already chosen. This keeps an expensive registry fallback from silently hiding a verified layer-diff advantage. Wendy and Compose are auto-detected; other systems can define output markers:

```sh
dlo deploy --root . --adapter generic \
  --phase-marker 'build=^Compiling' \
  --phase-marker 'readiness=^Service ready$' \
  -- ./deploy.sh
```

Commands and output logs are never persisted.

## Analyze and learn

```sh
dlo analyze --root /path/to/project
dlo analyze --root /path/to/project --json
dlo history --root /path/to/project --json
```

The analyzer maps `COPY` and `ADD` through BuildKit's parsed instruction model, respects Docker ignore rules, combines recency-weighted Git history with local observations, and ranks invalidation points by change likelihood × downstream comparative cost. A relevant non-build task can be recorded without storing its description:

```sh
dlo record --root . --kind task --status success --from-git --tag dependencies
```

## Privacy and state

Observations and short-lived proof records live outside the repository in the operating system user cache:

- macOS: `~/Library/Caches/docker-layer-optimizer/`
- Linux: `${XDG_CACHE_HOME:-~/.cache}/docker-layer-optimizer/`
- Windows: `%LOCALAPPDATA%/docker-layer-optimizer/`

Set `DLO_CACHE_DIR` to override the base directory. Successful proofs are retained for at most 30 days and 20 runs; failed proofs expire after seven days. DLO stores aggregate measurements, paths and hashes, IDs, coarse tags, and phase timings. It does not persist patches, commands, logs, source contents, prompts, secrets, environment values, or build-argument values.

See [SECURITY.md](SECURITY.md) and the [observation schema](docs/observation-schema-v3.json).

## Agent plugin

Codex or Claude supplies project judgment; DLO supplies deterministic measurement and application gates.

```sh
codex plugin marketplace add wendylabsinc/docker-layer-optimizer --ref main
```

Inside Codex, open `/plugins`, install **Docker Layer Optimizer**, and begin a new session. A useful first request is:

> Use optimize-docker-layers to observe this project's normal Docker workflow, explain the dominant bottleneck, and plan a measured optimization. Do not apply an unverified patch.

Claude Code:

```sh
claude plugin marketplace add wendylabsinc/docker-layer-optimizer
claude plugin install docker-layer-optimizer@docker-optimization-tools
```

The plugin is optional; agents can call the JSON CLI directly.

## Compatibility

| Capability | Requirement | Notes |
|---|---|---|
| Static analysis | Native `dlo`, Git | Docker is optional. |
| Dockerfile parsing | Moby BuildKit parser | Supports stages, flags, JSON form, continuations, heredocs, and `COPY --from`. |
| Structured step counts | Docker Buildx with `--progress=rawjson` | Plain fallback is less robust. |
| Local layer comparison | Successful `--load` exporter | Compares uncompressed DiffIDs. |
| Registry comparison | Readable pushed OCI/Docker manifest | Compares compressed blob digests and declared sizes. |
| Deployment profiling | Recognizable or custom output markers | Wendy and Compose adapters included. |
| Platforms | Linux, macOS, Windows; AMD64 and ARM64 | Static release binaries; CI cross-compiles all six targets. |

## Benchmarks and development

```sh
go test ./...
go vet ./...
go build ./cmd/dlo
```

Real-Docker lifecycle tests are opt-in:

```sh
DLO_DOCKER_INTEGRATION=1 go test ./internal/integration -v -count=1 -timeout=20m
```

The historical benchmark harness uses Python only as test orchestration; the DLO runtime is entirely Go:

```sh
python3 benchmarks/run_benchmarks.py --iterations 5 --output benchmark.json
```

Results are synthetic or project-specific, not universal performance claims. See the [methodology](docs/benchmarking.md), [Colima ARM64 result](benchmarks/results/2026-08-01-colima-arm64.md), [Woof end-to-end benchmark](benchmarks/results/2026-08-02-woof-end-to-end.md), and [agent-first proof](benchmarks/results/2026-08-03-agent-first-optimize.md).

## License

MIT
