# Agent-first deployment optimization

Status: accepted for implementation, 2026-08-03

## Product promise

DLO is a deployment regression profiler that learns how a repository changes,
identifies why iterative Docker deployments are slow, and proposes a Dockerfile
or build-context patch whose improvement it can verify.

The initial user is a developer repeatedly deploying medium-to-heavy Docker
applications to remote, edge, or bandwidth-constrained targets. Docker Buildx,
Docker Compose, Wendy, and the generic phase-marker adapter are the supported
initial paths.

## Agent contract

The versioned CLI JSON is the canonical agent interface. Codex and Claude skills
are thin interpreters over that deterministic interface; an MCP server is
deferred until the contract stabilizes.

The primary workflow is:

```sh
dlo optimize --root . --plan --json
dlo optimize --root . --candidate candidate.patch --json
```

Planning never mutates the project. Verification operates on disposable copies
of the exact current working state, including uncommitted files. A verified
candidate is applied only if affected files still match their pre-verification
hashes. An agent-authored candidate and a built-in candidate pass through the
same proof engine.

## Proof policy

A candidate may be applied automatically when all of the following hold:

1. Control and candidate images build successfully.
2. The configured project verification commands pass against the candidate.
3. At least three paired representative trials improve the candidate median by
   both 10 percent and 0.5 seconds.
4. The candidate p95 does not materially regress.
5. No protected product or security property changes.
6. Verification remains within the configured time budget and is expected to
   repay its cost within 20 representative deployments.

Performance alone is not correctness. Without a `.dlo.yml` verification
contract or explicit verification command, DLO may plan and benchmark a patch
but may not auto-apply it.

Approval is always required for base-image or dependency-version changes;
runtime user, privilege, capability, mount, port, networking, entrypoint,
command, health-check, secret, registry, signing, architecture, or build-platform
changes; and removals whose runtime use cannot be demonstrated.

## Project configuration

`.dlo.yml` is a small, reviewable, secret-free contract:

```yaml
version: 1
verification:
  commands:
    - python -m unittest discover -s tests
benchmark:
  trials: 3
  budget_seconds: 600
  min_relative_improvement: 0.10
  min_absolute_seconds: 0.5
  max_relative_regression: 0.10
  max_absolute_regression_seconds: 0.5
  payback_deploys: 20
  source_path: app.py
```

Existing Dockerfile or Compose health checks may inform future configuration
generation, but are not silently treated as a sufficient verification contract.

## Candidate scope

The first implementation can generate a conservative manifest-first candidate
for a broad `COPY .` immediately followed by recognized dependency installation.
Agents can submit any unified diff, but automatic application is limited to
Dockerfiles, Dockerfile-specific ignore files, `.dockerignore`, and Compose
configuration. Non-Docker bottlenecks produce a structured handoff to the local
agent rather than turning DLO into a general application profiler.

Target-side benchmarks require an explicitly configured isolated canary. They
must never replace the existing service. Initial automatic verification is
local-build based; canary orchestration is a later adapter capability.

## Learning, privacy, and retention

Each build or deployment may cheaply update local path and phase history.
Expensive optimization is explicit or agent-triggered; DLO never launches
surprise background builds. Agents may attach short structured intent tags, but
DLO does not retain prompts, issue text, commit messages, commands, logs, source
contents, secrets, or environment values.

Detailed proof records contain hashes, measurements, gates, and affected paths,
not patch contents. Successful proofs are retained for 30 days or the latest 20
runs per project, whichever retains fewer. Failed proofs expire after seven
days. Compact aggregate observations may remain. Shared sanitized profiles are
deferred.

## CI

CI is check-and-propose only. It may detect regressions and produce candidates
or proof artifacts, but it never modifies the repository. A local agent decides
what to investigate or apply.

## Non-goals for this release

- Shared team profiles or hosted telemetry.
- Kubernetes and cloud-vendor deployment adapters.
- Surprise background optimization.
- Automatic canary deployment to a real device.
- General application-startup or network optimization.
- PyPI stable status; the agent-first workflow ships as a beta first.
