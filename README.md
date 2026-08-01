# Docker Layer Optimizer

`dlo` is a deterministic CLI that measures Docker cache behavior and learns how a project changes. It counts cached and rebuilt Dockerfile steps, compares the resulting image layers with the previous successful build, and combines those observations with Git history to find expensive cache invalidation.

The included Codex and Claude skill is a thin agent interface over the same tool. Wendy is not required.

## Install the CLI

Python 3.9+, Git, and Docker Buildx are required for measured builds. Static analysis works without Docker.

```sh
python3 -m pip install git+https://github.com/OliverTaylor246/docker-layer-optimizer.git
```

Run a measured build:

```sh
dlo build --root /path/to/project --tag my-app:dev
```

The command streams the normal build output, then reports:

- cached, rebuilt, and failed Dockerfile steps from BuildKit plain progress;
- matching and unmatched immutable layer DiffIDs, plus changed ordered-chain positions and common-prefix length;
- build duration, transferred context size, and paths changed since the prior observed build attempt.

The default output is `--load`, which enables exact local image inspection. `--push` records BuildKit step measurements but deliberately leaves image-layer measurements unavailable rather than inspecting a possibly stale local tag.

Analyze the Dockerfile and project history:

```sh
dlo analyze --root /path/to/project
dlo analyze --root /path/to/project --json
```

Review observations:

```sh
dlo history --root /path/to/project
```

Pass common build settings directly:

```sh
dlo build --root . --dockerfile docker/Dockerfile --tag my-app:dev \
  --platform linux/amd64 --target runtime --build-arg VERSION=dev
```

## What it optimizes

- Maps `COPY` and `ADD` inputs to tracked project files.
- Estimates path change likelihood using recency-weighted Git history and local build/task observations.
- Ranks invalidation points by change likelihood × downstream comparative cost.
- Finds broad copies before dependency installation and missing `.dockerignore` files.
- Measures whether changes rebuild Dockerfile steps, introduce unmatched layer content, or change ordered layer-chain positions.

The analyzer recommends changes; it does not rewrite Dockerfiles automatically. Layer order is constrained by build semantics, so proposed changes should be reviewed and validated with representative builds.

## Local data and privacy

Observations live in the operating system's user cache, keyed by a hash of the canonical project path:

- macOS: `~/Library/Caches/docker-layer-optimizer/`
- Linux: `${XDG_CACHE_HOME:-~/.cache}/docker-layer-optimizer/`
- Windows: `%LOCALAPPDATA%/docker-layer-optimizer/`

Set `DLO_CACHE_DIR` to override the base directory. The tool stores paths, hashes, coarse tags, timings, digests, and counts. It does not store prompts, source contents, build logs, secrets, environment values, or build-argument values.

Manual task observations remain available:

```sh
dlo record --root . --kind task --status success --from-git --tag dependencies
```

## Install the agent skill

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

Ask the agent to use `optimize-docker-layers`. It will use `dlo` as the deterministic measurement and analysis engine, then apply human-readable reasoning to safe Dockerfile changes.

## Development

```sh
python3 -m unittest discover -s tests -v
python3 -m pip install -e .
dlo --help
```

## License

MIT
