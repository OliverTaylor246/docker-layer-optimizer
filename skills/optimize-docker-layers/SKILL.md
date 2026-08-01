---
name: optimize-docker-layers
description: Analyze and improve Dockerfile or Containerfile layer caching using Git change history and local build/task observations. Use when Docker builds or edge deployments are slow or expensive, when COPY/RUN ordering causes frequent cache invalidation, when creating or reviewing a container build, or when the user wants the build layout to learn from how a project changes over time.
---

# Optimize Docker Layers

Use the bundled analyzer to estimate which build-context inputs change together, which Docker layers they invalidate, and where expensive work should move. Treat its cost units as comparative estimates, not measured seconds.

## Analyze

1. Resolve this skill directory as `SKILL_DIR` and the project root as `PROJECT_ROOT`.
2. Inspect the Dockerfile or Containerfile, dependency manifests, `.dockerignore`, build command, and target architecture.
3. Run:

```sh
python3 "$SKILL_DIR/scripts/docker_layer_optimizer.py" analyze --root "$PROJECT_ROOT"
```

Use `--dockerfile path/to/Dockerfile` for a non-default build file and `--json` when structured output helps. Read [algorithm.md](references/algorithm.md) only when interpreting scores, extending the analyzer, or explaining its limitations.

4. Present the current high-risk layers and the smallest safe restructuring plan before editing. Prefer these patterns:
   - Copy dependency manifests and lockfiles before installing dependencies.
   - Copy volatile application source after stable, expensive dependency layers.
   - Group paths that commonly change together when doing so does not widen invalidation.
   - Keep unrelated volatile paths in separate late layers.
   - Use BuildKit cache mounts for package-manager and compiler caches.
   - Exclude irrelevant or generated content with `.dockerignore`.

## Change safely

- Preserve build semantics, stage boundaries, ownership, permissions, build arguments, and runtime contents.
- Never reorder `RUN`, `COPY`, or `ADD` instructions across an implicit dependency or side effect merely because a score suggests it.
- Treat `COPY --from=...` as a stage dependency, not a build-context copy.
- Check language-specific workspace behavior before splitting manifests. Rust workspaces, Node workspaces, Go replace directives, and monorepo package managers often require multiple manifests.
- Do not pin or replace base images unless the user asks.
- Make one coherent Dockerfile change at a time. Show the diff and explain the expected cache effect.
- Do not automatically rewrite a Dockerfile when the user requested analysis only.

## Verify

Run the project's normal container build when available. Compare at least two builds when practical: a warm no-op build and a build after changing a representative volatile file. Confirm the runtime artifact or tests still pass.

If actual timings or transfer bytes are available, record them:

```sh
python3 "$SKILL_DIR/scripts/docker_layer_optimizer.py" record \
  --root "$PROJECT_ROOT" --kind build --status success \
  --duration 42.3 --bytes-pushed 10485760 --invalidated-from 7
```

Omit unknown values rather than estimating them.

## Learn from work

After completing a relevant project task, record only coarse metadata:

```sh
python3 "$SKILL_DIR/scripts/docker_layer_optimizer.py" record \
  --root "$PROJECT_ROOT" --kind task --status success \
  --from-git --tag dependencies
```

Choose short tags such as `source`, `dependencies`, `config`, `assets`, `tests`, `build`, or `runtime`. Never store prompts, problem descriptions, source contents, secrets, or environment values. The script writes observations under the repository's Git metadata directory, so the learning profile remains local and cannot be committed accidentally.

Re-run `analyze` after meaningful tasks or deployments. Explain when recommendations changed because local observations became available.

## Report

Return:

1. The layer or input group causing the largest expected rebuild cost.
2. The evidence used: Git history depth, local observation count, and any measured build data.
3. The exact proposed or applied layer change.
4. Validation results.
5. Remaining uncertainty, especially dynamic `COPY` paths, generated files, remote caches, or unmeasured layer sizes.
