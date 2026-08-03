# Agent-first `dlo optimize` integration, 2026-08-03

This test exercised the installed `0.5.0b1` package against real Docker from
planning through automatic application. The fixture intentionally copied the
whole context before a one-second dependency step:

```dockerfile
FROM alpine:3.21
WORKDIR /app
COPY . .
RUN echo "pip install -r requirements.txt" > /dependency-proof && sleep 1
CMD ["cat", "app.py"]
```

DLO generated a manifest-first candidate, created disposable control and
candidate snapshots, warmed both layouts, performed three paired unique source
edits, performed no-op and dependency-change negative controls, ran the
configured Python syntax check, and applied the candidate only after every gate
passed.

| Measurement | Control | Candidate | Improvement |
| --- | ---: | ---: | ---: |
| Source-change median, 3 paired trials | 1.448 s | 0.378 s | 1.070 s / 73.9% |
| Complete one-time verification | | 10.222 s | Break-even in 9.6 source deploys |

The automatically applied result was:

```dockerfile
FROM alpine:3.21
WORKDIR /app
COPY ["requirements.txt", "./"]
RUN echo "pip install -r requirements.txt" > /dependency-proof && sleep 1
COPY . .
CMD ["cat", "app.py"]
```

The test also checks that the proof record contains neither the patch nor the
verification command. A repeated integration run initially exposed stable
synthetic edit markers incorrectly reusing cache from an earlier run; the
benchmark now uses a unique per-operation ID and the repeated run above passed.

Environment: arm64 macOS 26.5.2, Docker client 29.7.1, Docker server 29.5.2,
Buildx 0.36.0. This deterministic fixture validates the proof workflow and cache
mechanism; it is not a universal speed claim. The existing Woof benchmark
remains the representative remote-device result.
