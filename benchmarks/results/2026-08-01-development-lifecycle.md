# Development lifecycle baseline: G1 and Woof, 2026-08-01

This baseline answers a different question from the synthetic control-versus-optimized matrix:

> What normally happens while repeatedly developing and deploying the same project?

The answer is warm-cache deployment. A cold build is a first-build or cache-loss event, not the counterfactual for every source edit. DLO did not change a Dockerfile in these measurements, so the speedup attributable to the wrapper is **0%**. Docker and Wendy provided the cache reuse; DLO measured and explained it.

Machine-readable measurements are in [2026-08-01-development-lifecycle.json](2026-08-01-development-lifecycle.json).

## Results

| Path | Event | Runs | Median or observed time | Cache/layer evidence |
|---|---|---:|---:|---|
| G1 Xbox, historical Wendy deployment | First project build | 1 | about 63.6 s | Project dependency stages rebuilt |
| G1 Xbox, historical Wendy deployment | Source/config development deploy | 8 | 8.924 s | 7/8 device layers reused; roughly 67 KiB app layer applied |
| G1 Xbox, controlled local ARM64 build | Cold Docker builder | 1 | 263.287 s | 9 steps rebuilt |
| G1 Xbox, controlled local ARM64 build | Source edit | 3 | 1.492 s | 8/10 steps cached; 7/8 image layers reused |
| G1 Xbox, controlled local ARM64 build | Dependency manifest edit | 1 | 33.729 s | native CycloneDDS layer cached; 5/8 layers reused |
| Woof control agent, live Wendy deployment | First deployment | 1 | 18.75 s | 4/6 device layers reused |
| Woof control agent, live Wendy deployment | No-op | 1 | 7.02 s | all build steps cached |
| Woof control agent, live Wendy deployment | Source edit | 3 | 6.52 s | 5/6 device layers reused |

The historical G1 warm-deploy range was 7.451–10.038 seconds with an 8.903-second mean. Those commands included building, pushing, replacing the container, and waiting for the service to become ready. The surrounding Codex tasks were longer because they also edited code, ran 34–37 tests, verified live controller state, committed, and pushed.

The controlled local G1 measurements used a different builder and output path from the historical Wendy deployments. They demonstrate cache decisions, not a before/after deployment-speed comparison. In particular, the 263-second cold Docker build must not be presented as the normal without-DLO source-edit baseline.

## Heavy application finding

Collie showed why layer counts alone are insufficient:

- one historical deployment failed because two ignored model files referenced by the Dockerfile were missing from the local context;
- after recovering the artifacts, a build spent about six minutes unpacking the large Jetson/PyTorch base and uploading layers;
- later UI/source deployments took about 127–130 seconds even with cache reuse because image export, transfer, container restart, and GPU/model warmup remained;
- a separate cold Docker-builder attempt failed after 382.39 seconds when the builder reached its 14.9 GiB storage ceiling.

A useful deployment tool therefore needs phase-level timing and capacity checks in addition to Dockerfile analysis.

## Required baseline for future claims

Every project benchmark should record these separately:

1. First project build with the normal persistent builder.
2. At least five no-op warm deployments.
3. At least five representative source-only deployments.
4. At least three dependency-manifest deployments.
5. One explicit cache-loss recovery run, labeled as such.
6. Build, export, transfer, device unpack, container replacement, and readiness/warmup time.

Before/after optimization comparisons must use the same machine, builder, platform, output path, cache policy, and deployment target. Report median and p95. A DLO speedup may only be claimed when DLO recommends a concrete change, the change is applied, and the same lifecycle workload measurably improves.

## Product conclusion

The defensible claim is:

> DLO does not replace Docker caching. It measures cache behavior, identifies and verifies concrete layout improvements, detects regressions, and explains why a deployment became slow.

For already well-layered projects such as G1 Xbox Controller, the expected result is verification and regression protection—not a fabricated optimization percentage.
