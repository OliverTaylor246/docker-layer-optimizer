# Docker Layer Optimizer

A history-aware Codex and Claude skill for reducing Docker rebuild and redeploy cost.

It combines static Dockerfile analysis with the way your repository actually changes. Git history supplies the initial signal; optional local observations improve it as you work. The optimizer ranks cache invalidation risk, identifies volatile project areas and co-changing paths, and guides an agent through safe layer restructuring.

## What it does

- Maps `COPY` and `ADD` inputs to tracked project files.
- Estimates change likelihood with recency-weighted Git history.
- Ranks layers by change likelihood × downstream rebuild cost.
- Detects broad copies before dependency installation and missing `.dockerignore` files.
- Learns from optional task, build, and deploy observations.
- Stores learning data inside `.git/docker-layer-optimizer/`, never in the repository.
- Records paths and coarse tags, not prompts, source contents, secrets, or environment values.

The current cost model is comparative. Build duration and bytes pushed are recorded when available, but the analyzer does not yet ingest BuildKit traces automatically.

## Install in Codex

```sh
codex plugin marketplace add OliverTaylor246/docker-layer-optimizer
codex plugin add docker-layer-optimizer@docker-optimization-tools
```

Start a new task and ask:

```text
Use $optimize-docker-layers to analyze this project and improve its Docker cache behavior.
```

## Install in Claude Code

```sh
claude plugin marketplace add OliverTaylor246/docker-layer-optimizer
claude plugin install docker-layer-optimizer@docker-optimization-tools
```

Reload plugins, then invoke `/docker-layer-optimizer:optimize-docker-layers` or ask Claude to optimize the Docker layers.

## Run the analyzer directly

The analyzer requires Python 3.9+ and Git; it has no third-party dependencies.

```sh
python3 skills/optimize-docker-layers/scripts/docker_layer_optimizer.py analyze --root /path/to/project
```

Machine-readable output:

```sh
python3 skills/optimize-docker-layers/scripts/docker_layer_optimizer.py analyze --root /path/to/project --json
```

Record a completed task using current uncommitted paths:

```sh
python3 skills/optimize-docker-layers/scripts/docker_layer_optimizer.py record \
  --root /path/to/project --kind task --status success --from-git --tag dependencies
```

Record measured build information:

```sh
python3 skills/optimize-docker-layers/scripts/docker_layer_optimizer.py record \
  --root /path/to/project --kind build --status success \
  --duration 42.3 --bytes-pushed 10485760 --invalidated-from 7
```

## Development

```sh
python3 -m unittest discover -s tests -v
python3 /path/to/skill-creator/scripts/quick_validate.py skills/optimize-docker-layers
python3 /path/to/plugin-creator/scripts/validate_plugin.py .
```

Claude Code can load the checkout directly with `claude --plugin-dir .`.

## License

MIT
