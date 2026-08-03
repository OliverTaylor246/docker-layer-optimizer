# Agent-first optimizer: native Docker capabilities and DLO's gap

Research date: 2026-08-03

This note uses first-party Docker, OpenAI, and Anthropic documentation. The product conclusions are explicitly marked as inferences rather than claims made by those sources.

## Executive conclusion

Docker already supplies the mechanisms DLO needs to observe and validate builds:

- BuildKit emits machine-readable solve events and stores per-build records.
- Docker documents cache invalidation rules and conventional cache improvements.
- Build checks lint Dockerfiles against predefined correctness and style rules.
- Docker Scout analyzes image composition and supply-chain security.
- Compose provides enough namespacing, health, and port primitives to create many isolated canary deployments.

The documented native tools do not close the product loop DLO is targeting: learn how a particular repository changes over time, attribute complete deployment latency, generate a candidate change, run representative paired benchmarks, verify a project-specific equivalence contract, estimate payback, and decide whether to apply the patch. That longitudinal verification loop—not another Dockerfile linter—is DLO's differentiated core.

## What Docker already provides

### 1. Cache semantics and established optimization advice

BuildKit checks the base image and then compares each Dockerfile instruction with cached layers. `ADD`, `COPY`, and bind-mounted `RUN` instructions include relevant file metadata in their cache checksum. Once a cache miss occurs, later Dockerfile commands are rebuilt. Docker therefore recommends ordering instructions from less frequently changed to more frequently changed. File modification time alone does not invalidate `ADD` or `COPY` cache entries. [`RUN` cache entries are not refreshed merely because remote package data has changed](https://docs.docker.com/build/cache/invalidation/).

Docker's optimization guide already recommends ordering layers, reducing build context with `.dockerignore`, using bind or cache mounts, and using external caches. It explains that a smaller context reduces transfer and the chance of invalidation. These are established mechanisms; DLO should not market them as novel discoveries. [Docker: Optimize cache usage in builds](https://docs.docker.com/build/cache/optimize/).

**Product implication (inference):** DLO's value is not knowing the generic rule “stable inputs first.” It is measuring which inputs are actually stable in this repository, identifying the expensive descendants invalidated by its real work, and proving that a proposed reorder improves representative deployments.

### 2. Structured, per-build observability

`docker buildx build --progress=rawjson` emits BuildKit solve-status events as JSON Lines and is explicitly designed for external programs. The same command can write a JSON metadata file containing the build reference, image digests, descriptor, provenance, and optional warnings. [Docker: `buildx build` progress](https://docs.docker.com/reference/cli/docker/buildx/build/#set-type-of-progress-output---progress), [Docker: build metadata file](https://docs.docker.com/reference/cli/docker/buildx/build/#write-build-result-metadata-to-a-file---metadata-file).

Buildx also retains build records. `docker buildx history inspect --format json` exposes duration, status, build inputs, platforms, outputs, total steps, and cached-step count; history can be queried for the current repository. [Docker: `buildx history inspect`](https://docs.docker.com/reference/cli/docker/buildx/history/inspect/), [Docker: `buildx history ls`](https://docs.docker.com/reference/cli/docker/buildx/history/ls/).

**Product implication (inference):** DLO should consume these native structured interfaces where available rather than parse human TTY output. It can supplement them with image DiffIDs, registry/transfer observations, and deployment-adapter timing. Its own persisted history should focus on information Buildx does not associate into a project optimization model: change class, changed-path patterns, target class, deployment phases, candidate identity, equivalence results, amortization, and the decision taken.

**Compatibility implication (inference):** Buildx history is builder-scoped, and fields can vary with Docker/Buildx versions and build configuration. DLO should treat raw solve events as the primary live measurement and build history/metadata as enrichment, not as the only source of truth.

### 3. Docker build checks are linting, not empirical optimization

BuildKit's build checks analyze build configuration against predefined rules. `docker build --check .` evaluates checks without producing an image and exits non-zero when violations are found. During a normal build, violations are warnings unless configured as errors. The current rules cover issues such as casing, undefined arguments/variables, secrets in `ARG` or `ENV`, relative workdirs, invalid platform declarations, ignored copied files, and shell-form command concerns. [Docker: Build checks](https://docs.docker.com/reference/build-checks/), [Docker: Checking build configuration](https://docs.docker.com/build/checks/).

**Product implication (inference):** DLO should run or surface native build checks as a prerequisite signal, not duplicate them. A clean build-check result does not show that a Dockerfile matches a repository's change distribution, makes iterative deployments cheaper, or preserves runtime behavior after a candidate patch. DLO's report should distinguish `native_checks` from `optimization_proof`.

### 4. Docker Scout solves a different problem

Docker Scout builds an SBOM, matches packages against vulnerability data, evaluates supply-chain policies, and recommends base-image remediation. Its CLI includes CVE analysis, policy evaluation, SBOM generation, image comparison, and base-image recommendations. [Docker Scout overview](https://docs.docker.com/scout/), [Docker Scout CLI](https://docs.docker.com/reference/cli/docker/scout/), [Docker Scout image recommendations](https://docs.docker.com/scout/explore/image-details-view/#remediation-recommendations).

**Product implication (inference):** Scout is adjacent safety evidence, not a competitor for iterative deployment optimization. DLO should not automatically change base images or dependencies as a speed optimization. When available, Scout policy/CVE results can be an optional guardrail for a candidate, but DLO's core should work without a Docker account or Scout service.

## Compose canary verification: useful primitives and limits

Compose project names group and isolate resources and allow the same Compose model to be deployed more than once. A project name can be supplied with `-p` or `COMPOSE_PROJECT_NAME`. [Docker: Compose application model](https://docs.docker.com/compose/intro/compose-application-model/), [Docker: project names](https://docs.docker.com/reference/cli/docker/compose/#use--p-to-specify-a-project-name).

Compose health checks define how container health is evaluated. `docker compose up --wait` creates and starts containers, then waits for services to be running or healthy and supports a timeout. A port declared with only the container port lets the runtime allocate an available host port; `docker compose port` reports the binding. [Docker: Compose service health checks and ports](https://docs.docker.com/reference/compose-file/services/#healthcheck), [Docker: `compose up --wait`](https://docs.docker.com/reference/cli/docker/compose/up/), [Docker: `compose port`](https://docs.docker.com/reference/cli/docker/compose/port/).

These primitives support a DLO canary pattern:

1. Render a temporary Compose override with candidate image tags and resource limits.
2. Use a unique, normalized Compose project name.
3. Remove or replace fixed host-port bindings with loopback-only dynamic bindings when safe.
4. Start detached and wait for health, then run the configured smoke test against the discovered port.
5. Collect status and timing, then run `compose down` for that exact project.

**Limits (inference from the Compose model):** a distinct project name cannot guarantee isolation when the application uses explicitly named or external resources, `network_mode: host`, host bind mounts with side effects, fixed host ports, privileged hardware access, or external services. DLO must preflight the resolved Compose model and refuse automatic target-side verification unless it can demonstrate isolation. Health is also only as meaningful as the configured check; “running” is not functional equivalence.

## Agent-first interface findings

OpenAI function tools use JSON Schema for parameters and can enforce strict schema adherence. Structured Outputs likewise support a strict JSON Schema response format. [OpenAI Responses API reference](https://platform.openai.com/docs/api-reference/responses/create).

Anthropic tools also use JSON Schema for `input_schema`. Its first-party guidance recommends detailed behavioral descriptions, schema-valid examples for complex inputs, fewer consolidated tools, meaningful namespacing, stable semantic identifiers, and high-signal responses. Strict tool use can guarantee schema-valid tool inputs. [Claude: Define tools](https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools).

**Product implication (inference):** a versioned CLI/JSON contract is the correct canonical interface before an MCP server. A thin agent skill can call it today, and the same schemas can later back a strict function or MCP tool.

Recommended interaction shape:

```text
dlo optimize --root . --plan --json
dlo optimize --root . --apply-approved <candidate-id> --json
```

The plan operation should be read-only and return a stable candidate ID, evidence, estimated verification cost, estimated savings, break-even deployment count, risks, required checks, and an explicit next action. Application should require that candidate ID, verify preimage hashes, and return whether the candidate was applied, rejected, stale, or still unproven. Human prose should be a renderer over the same data rather than a separate semantic interface.

The JSON contract should include:

- `schema_version` and `tool_version`;
- `operation_id`, stable `candidate_id`, and coarse target identifier;
- a closed status enum and machine-actionable `next_actions`;
- separation between observations, inferences, and proof results;
- timings with units and sample counts, not formatted strings;
- correctness checks and protected-property comparisons;
- mutation status, affected paths, preimage hashes, and rollback guidance;
- explicit privacy/retention metadata;
- exit codes mapped to broad categories such as success, no worthwhile candidate, proof failure, stale candidate, configuration required, and infrastructure failure.

## The gap DLO can credibly own

| Capability | Native Docker | DLO's differentiated responsibility |
| --- | --- | --- |
| Explain cache mechanics | Documents deterministic invalidation rules and general best practices | Relate those rules to the repository's observed change distribution |
| Inspect one build | Raw BuildKit JSON, metadata, and Buildx history | Normalize observations across builds, targets, deploy phases, and task tags |
| Lint a Dockerfile | Predefined BuildKit build checks | Generate optimization candidates and evaluate project-specific impact |
| Analyze image security | Scout SBOM, CVEs, policies, and base-image recommendations | Keep security-sensitive changes approval-only and optionally consume policy results as guardrails |
| Isolate a Compose run | Project names, dynamic ports, health checks | Prove the resolved model is safe for a canary, execute it, and clean it up |
| Decide whether to edit | No documented longitudinal A/B decision loop in the reviewed features | Paired representative trials, equivalence contract, noise threshold, payback gate, and safe apply |

The primary promise should therefore remain:

> DLO is a deployment regression profiler that learns how a repository changes, identifies why iterative remote or edge deployments are slow, and proposes a Docker-related patch whose performance and functional equivalence it can verify before automatic application.

## Requirements this research suggests for `dlo optimize`

1. Prefer BuildKit `rawjson`, metadata files, and history JSON; use text parsing only as a declared fallback.
2. Run native Docker build checks and report them separately from optimization proof.
3. Infer benchmark scenarios from local changed-path history and structured task tags, without retaining prompts or source content.
4. Gate expensive verification on predicted payback, then use paired control/candidate trials and report sample counts and noise.
5. Require build success plus the configured tests, health checks, smoke checks, and protected runtime properties before auto-apply.
6. Benchmark in disposable snapshots and apply only if affected-file preimages still match.
7. Require a successful canary-isolation preflight before target-side verification; otherwise remain local and mark remote performance unverified.
8. Keep base image, dependency, privilege, secret, architecture, entrypoint, port/network, and health-semantic changes approval-only.
9. Make CI check-and-propose only; reserve mutation for an explicit local agent operation.
10. Return one versioned, high-signal JSON proof model that can later become a strict agent tool with no semantic rewrite.

## Open validation questions

- Which Buildx versions and builder drivers reliably expose the required raw events and history fields?
- Which Compose constructs can be safely rewritten into an isolated canary automatically, and which must force local-only verification?
- How well do change-frequency predictions hold across common Python, Node, Go, Rust, Java, and monorepo histories?
- What paired-trial strategy best controls warm-cache ordering bias on remote targets?
- Which correctness checks are cheap enough to run by default without making the payback model self-defeating?
