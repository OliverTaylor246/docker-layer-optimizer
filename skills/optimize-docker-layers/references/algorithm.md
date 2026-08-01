# Scoring and data model

## Inputs

The analyzer reads:

- Logical `FROM`, `COPY`, `ADD`, and `RUN` instructions from a Dockerfile or Containerfile.
- Paths from `git ls-files`.
- Per-commit changed-path sets from recent Git history.
- Optional local observations recorded under `.git/docker-layer-optimizer/events.jsonl`.

It never reads file contents for learning and never stores prompts.

## Change likelihood

Recent commits receive exponentially decaying weights. For a `COPY` source, change likelihood is the weighted fraction of commits in which any matching tracked path changed. When local observations exist, the score blends Git history with more recent task/build observations.

The report also groups changes by top-level path. Co-change pairs use Jaccard similarity: commits changing both groups divided by commits changing either group.

## Invalidation cost

Each instruction after a context `COPY` or `ADD` receives a comparative cost. Dependency installation and compilation have higher defaults than ordinary shell commands; copying data has a smaller cost. A layer's risk is:

```text
change likelihood × downstream comparative cost
```

These units rank opportunities within one Dockerfile. They are not seconds, bytes, or a cloud bill.

## Limits

- Static parsing cannot fully evaluate build arguments, shell expansion, heredocs, or files generated before a build.
- Git history can underrepresent uncommitted or newly introduced workflows.
- A directory probability says that something below it changed, not that every byte changed.
- Docker cache behavior also depends on metadata, base-image changes, builder configuration, remote cache availability, secrets, mounts, and platform.
- Reordering two copies is only safe when neither has a semantic dependency on the other or on intervening commands.

Prefer measured warm-build and representative-edit timings when they disagree with heuristic scores.
