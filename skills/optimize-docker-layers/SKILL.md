---
name: optimize-docker-layers
description: Measure, analyze, and improve Dockerfile or Containerfile layer caching using deterministic BuildKit observations, resulting image layer DiffIDs, Git change history, and local task history. Use when Docker builds or deployments are slow or expensive, COPY/RUN ordering causes cache invalidation, a container build needs review, or layer layout should adapt to how a project changes over time.
---

# Optimize Docker Layers

Use `dlo` as the source of measured facts and the bundled script as a fallback. Use agent judgment only to interpret evidence, identify semantic constraints, and make safe edits.

## Analyze

1. Resolve the project root, Dockerfile or Containerfile, dependency manifests, `.dockerignore`, build command, and target architecture.
2. Run the installed CLI if available:

```sh
dlo analyze --root "$PROJECT_ROOT"
```

Otherwise resolve this skill directory as `SKILL_DIR` and run:

```sh
python3 "$SKILL_DIR/scripts/docker_layer_optimizer.py" analyze --root "$PROJECT_ROOT"
```

Use `--dockerfile path/to/Dockerfile` for a non-default file and `--json` for structured output. Read [algorithm.md](references/algorithm.md) when interpreting scores, extending the tool, or explaining limitations.

3. Present the highest-risk invalidation point and the smallest safe restructuring plan before editing. Prefer dependency manifests before dependency installation, volatile source after stable expensive work, unrelated volatile inputs in separate late copies, useful BuildKit cache mounts, and an effective `.dockerignore`.

## Measure

When the user authorizes a build and Docker Buildx is available, create a baseline and repeat after a representative change:

```sh
dlo build --root "$PROJECT_ROOT" --tag project-name:dlo
dlo history --root "$PROJECT_ROOT"
```

The build command deterministically counts Dockerfile step cache outcomes from BuildKit plain progress. For its default loaded output, it also compares exact image layer DiffIDs with the previous successful build. Use `--push` only when requested; pushed builds cannot provide local image-layer comparison unless separately loaded.

Never estimate missing timings, layer counts, or bytes. Do not claim that a Dockerfile instruction maps one-to-one to an image layer; BuildKit steps and resulting image layers are separate measurements.

## Change safely

- Preserve build semantics, stage boundaries, ownership, permissions, arguments, mounts, secrets, and runtime contents.
- Never reorder instructions across an implicit dependency or side effect merely because a score suggests it.
- Treat `COPY --from=...` as a stage dependency, not a build-context copy.
- Check language-specific workspace behavior before splitting manifests. Rust, Node, Go, Python, and other monorepos can require several workspace files together.
- Do not pin or replace base images unless asked.
- Make one coherent Dockerfile change at a time and explain the expected cache effect.
- Do not edit when the user requested analysis only.

## Learn from work

Measured builds automatically snapshot the effective local context and record changed paths on the next successful or failed run. To record a relevant non-build task:

```sh
dlo record --root "$PROJECT_ROOT" --kind task --status success --from-git --tag dependencies
```

Use short coarse tags such as `source`, `dependencies`, `config`, `assets`, `tests`, `build`, or `runtime`. Never store prompts, problem descriptions, source contents, secrets, logs, environment values, or build-argument values.

Observations are stored in the operating system's user cache, outside the repository, with `DLO_CACHE_DIR` available as an override. Re-run `analyze` after meaningful tasks or builds and say when local observations changed a recommendation.

## Report

Return the largest expected rebuild cost, evidence depth, measured cache and layer counts when available, the exact proposed or applied change, validation results, and remaining uncertainty such as dynamic paths, generated files, remote caches, or unmeasured pushed-image layers.
