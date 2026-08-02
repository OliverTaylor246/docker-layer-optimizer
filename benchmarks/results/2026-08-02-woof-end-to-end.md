# Woof end-to-end development benchmark, 2026-08-02

This benchmark measured a full local-development deployment path through Wendy to the ARM64 device named Woof: build, image export and push, device pull and unpack, container replacement, and TCP readiness. It compared the same FastAPI application and pinned dependencies under two Dockerfile layouts:

```dockerfile
# Control
COPY . .
RUN pip install --no-cache-dir -r requirements.txt
```

```dockerfile
# Optimized
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
```

Both projects used the same `python:3.11-slim` base, FastAPI 0.116.1, Uvicorn 0.35.0, `.dockerignore` policy, Wendy CLI, Apple Container builder, LAN device, readiness behavior, and host-network mode. Distinct app names and ports allowed the caches and containers to coexist without modifying Woof's existing applications.

## Results

| Change type | Runs per layout | Control median | Optimized median | Median difference | Cache/layer result |
|---|---:|---:|---:|---:|---|
| Warm no-op | 5 | 3.29 s | 2.95 s | 0.34 s | All Docker steps cached in both; means were 3.148 s and 3.168 s, so no speedup is attributed |
| Source-only edit | 5 | 11.08 s | 4.58 s | **6.50 s / 58.7% faster** | Control rebuilt dependencies and reused 5/7 device layers; optimized cached dependencies and reused 7/8 |
| Dependency-manifest edit | 3 | 13.75 s | 13.24 s | 0.51 s / 3.7% | Both rebuilt and applied dependencies; treated as no meaningful improvement |

For source edits, Wendy's reported build median fell from 6.523 seconds to 2.114 seconds, a 67.6% reduction. More importantly, the device changed from applying an approximately 8.6 MiB dependency layer on every source edit to reusing that layer and applying only a 265–268 byte source layer.

Source-edit end-to-end observations were:

- control: 10.53, 9.45, 11.08, 11.08, and 11.47 seconds;
- optimized: 4.18, 4.45, 4.58, 7.39, and 5.89 seconds.

The optimized outliers show that image push and replacement variance remains even after Docker invalidation is corrected. Layer identity and repeated medians are therefore more trustworthy than a single deployment time.

## First deployments and negative control

The first deployments took 10.29 seconds for the control and 9.58 seconds for the optimized layout. They are reported as cache-warming observations, not as a before/after performance claim.

Three dependency-manifest edits deliberately invalidated dependency installation in both layouts. Their medians differed by only 3.7%, and both repeatedly applied the approximately 8.6 MiB dependency layer. This supports the intended claim: the optimization helps frequent source changes, not genuine dependency changes.

## Setup failure retained as evidence

The first warm measurement attempt was excluded because the benchmark manifests initially omitted Wendy's host-network entitlement. Docker completed a fully cached build in 0.594 seconds, but readiness timed out after 60 seconds because the container was not reachable at the probed host address. After correcting networking, both caches were rewarmed and the complete matrix was restarted.

This failure is useful product evidence: Docker-layer optimization cannot fix container networking, replacement, or readiness problems. A deploy profiler should report those phases separately instead of calling the entire deployment a slow build.

## Cleanup and scope

Both isolated benchmark applications and their device images were removed after successful response checks. Existing Woof workloads were not modified. The machine-readable observations are in [`2026-08-02-woof-end-to-end.json`](2026-08-02-woof-end-to-end.json).

This is one device, workload, builder, and LAN. It validates the mechanism and the development-edit use case; it is not a universal 58.7% promise.
