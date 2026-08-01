# Measurement, scoring, and data model

## Deterministic build observations

`dlo build` invokes `docker buildx build --progress=plain`. It parses numbered Dockerfile vertices with `[x/y]` or `[stage x/y]` labels and their `CACHED`, `DONE`, or `ERROR` outcomes. Internal BuildKit vertices are excluded from Dockerfile-step counts.

For the default `--load` output, the tool inspects `RootFS.Layers` after a successful build. These immutable DiffIDs are compared with the prior successful observation for the same project, Dockerfile, tag, platform, target, and output mode. Multiset comparison reports matching and unmatched layer content; ordered comparison separately reports changed chain positions and common-prefix length. This distinction matters when identical DiffIDs move to different positions. The first build is explicitly marked as a baseline.

Failed builds advance the context-change snapshot so repeated attempts do not count the same edit repeatedly, but they never replace the last successfully inspected image-layer baseline. A `--push` build does not inspect the local tag because it may be stale, so image-layer fields remain unavailable. Dockerfile step counts and image layers are intentionally separate: a build step need not create exactly one filesystem layer.

Before each build, the tool hashes regular files in its approximation of the effective local context while excluding `.git` and common `.dockerignore` matches. It stores paths and hashes, never file contents. Comparing snapshots yields changed paths for the learning model.

## Static inputs

The analyzer reads logical `FROM`, `COPY`, `ADD`, and `RUN` instructions, paths from `git ls-files` filtered through the applicable root or Dockerfile-specific ignore file, changed-path sets from recent commits, and optional local observations from the user's operating-system cache. Dockerfile and ignore-file paths are excluded from context-copy matching because Docker does not make them available to `COPY`.

## Change likelihood

Recent commits and observations receive exponentially decaying weights. For a context input, change likelihood is the weighted fraction of change sets in which any matching tracked path changed. With both sources available, Git history contributes 70% and local observations 30%.

Top-level volatility uses the same weighted probability. Co-change pairs use Jaccard similarity: changes containing both groups divided by changes containing either group.

## Comparative invalidation score

Each instruction downstream of a context `COPY` or `ADD` receives a comparative cost. Dependency installation and compilation receive higher defaults than ordinary shell commands. Risk is:

```text
change likelihood × downstream comparative cost
```

These units rank opportunities within one Dockerfile; they are not seconds, bytes, image layers, or a cloud bill. Measured build fields remain independently visible in the evidence.

## Local state

State is keyed by a hash of the canonical project path and stored under the user cache (`~/Library/Caches`, `XDG_CACHE_HOME`, or `LOCALAPPDATA`). `DLO_CACHE_DIR` overrides the base. Events are JSON Lines; context and successful image baselines are stored atomically in `snapshot.json`.

## Limits

- Static parsing cannot fully evaluate arguments, shell expansion, heredocs, generated contexts, or remote Git contexts.
- The `.dockerignore` snapshot matcher implements common patterns but is not a replacement for Docker's full pattern-matching implementation.
- Git history can underrepresent new or uncommitted workflows.
- Cache behavior also depends on base-image changes, builder configuration, remote cache availability, secrets, mounts, platform, and exporters.
- DiffID reuse measures filesystem layer identity, not registry transfer bytes or compressed blob identity.
- Reordering is safe only when instructions have no semantic dependency on one another or on intervening commands.

Prefer repeated measured builds when they disagree with heuristic scores.
