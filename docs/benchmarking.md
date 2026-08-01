# Benchmark methodology

The benchmark matrix contains small Python, Node, Go, and Python-monorepo projects. Each has two semantically equivalent Dockerfiles:

- **control:** a broad context copy occurs before dependency work;
- **optimized:** dependency manifests are copied and installed before volatile source.

For each project and layout, the harness adds a per-run semantic comment/metadata salt to avoid accidental reuse from an earlier benchmark, warms a baseline, then performs repeated source edits and repeated manifest edits. It records wall time, cached/rebuilt BuildKit steps, and matching/unmatched image DiffIDs. It also alternates raw `docker buildx build` no-op runs with `dlo build` no-op runs and reports both wall time and DLO's measured snapshot-plus-inspection overhead.

The default is five repetitions. Reported p95 is the nearest-rank percentile, which is intentionally conservative for such a small sample. The harness records OS, machine architecture, Python, Docker, and Buildx versions and removes its local image tags afterward.

Run all cases:

```sh
python benchmarks/run_benchmarks.py --iterations 5 --output benchmark.json
```

Run one case while iterating:

```sh
python benchmarks/run_benchmarks.py --cases python --iterations 2
```

Do not compare results from different builders or machines as if they were controlled. Network pulls, registry mirrors, CPU contention, filesystem performance, base-image changes, emulation, and cache garbage collection all affect wall time. The primary correctness signals are cache decisions and layer identity; elapsed-time savings require repeated measurements on the deployment path that matters.
