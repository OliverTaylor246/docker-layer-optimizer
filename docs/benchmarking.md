# Benchmark methodology

The benchmark matrix contains small Python, Node, Go, and Python-monorepo projects. Each has two semantically equivalent Dockerfiles:

- **control:** a broad context copy occurs before dependency work;
- **optimized:** dependency manifests are copied and installed before volatile source.

For each project and layout, the harness adds a per-run semantic comment/metadata salt to avoid accidental reuse from an earlier benchmark, warms a baseline, then performs repeated source edits and repeated manifest edits. It records wall time, cached/rebuilt BuildKit steps, and matching/unmatched image DiffIDs. It also alternates raw `docker buildx build` no-op runs with `dlo build` no-op runs and reports both wall time and DLO's measured snapshot-plus-inspection overhead.

The default is five repetitions. Reported p95 is the nearest-rank percentile, which is intentionally conservative for such a small sample. The harness records OS, machine architecture, Python, Docker, and Buildx versions and removes its local image tags afterward.

The synthetic matrix answers whether a specific manifest-first restructuring improves cache reuse. It does not model the frequency of cold builds during normal development. For that, use a development-lifecycle profile with one explicitly labeled first build, repeated no-op and source edits on the persistent cache, dependency edits, and a separately labeled cache-loss recovery run. End-to-end deployment profiles must split build, export, transfer, device unpack, replacement, and readiness/warmup when the deployment system exposes those phases.

Use the deployment profiler for each lifecycle repetition:

```sh
dlo deploy --root . --target test-device -- DEPLOYMENT COMMAND
dlo history --root .
```

Wendy and Docker Compose markers are built in. Other systems require explicit `--phase-marker 'PHASE=REGEX'` values. Because these durations are attributed from received output lines, retain unclassified time and do not present marker timings as native platform traces.

Run all cases:

```sh
python benchmarks/run_benchmarks.py --iterations 5 --output benchmark.json
```

Run one case while iterating:

```sh
python benchmarks/run_benchmarks.py --cases python --iterations 2
```

Do not compare results from different builders or machines as if they were controlled. Network pulls, registry mirrors, CPU contention, filesystem performance, base-image changes, emulation, and cache garbage collection all affect wall time. The primary correctness signals are cache decisions and layer identity; elapsed-time savings require repeated measurements on the deployment path that matters.

Do not use a cold build as the without-DLO baseline for every source edit. Wrapping an unchanged build has no expected speed benefit. Attribute a speedup to DLO only when it recommends a concrete Dockerfile or context change, that change is applied, and the same warm development workload improves under controlled conditions.

See the [G1 and Woof development-lifecycle baseline](../benchmarks/results/2026-08-01-development-lifecycle.md) for a real-project example and the distinction between existing Docker/Wendy cache reuse and DLO-attributable improvement.
