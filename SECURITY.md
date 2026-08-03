# Security and privacy

## Reporting

Please report vulnerabilities privately with GitHub Security Advisories for this repository. Do not include active credentials in an issue.

## Threat model

`dlo build` executes the project's Dockerfile through the local Docker Buildx installation. `dlo deploy -- COMMAND` executes the provided command directly without a shell. `dlo optimize` builds disposable copies of the current project and executes verification commands from `.dlo.yml` or explicit flags through the operating-system shell. Dockerfiles, deployment commands, candidate patches, and verification commands are executable input and should be treated as untrusted code. The tool does not protect against a malicious command, Docker daemon, builder, registry, repository, base image, or deployment target.

Optimization snapshots isolate file mutation, not code execution: builds still reach the configured Docker daemon and verification commands run with the user's permissions. Candidate snapshots include uncommitted and untracked project files so that control and candidate represent the same current state. DLO excludes `.git`, applies candidates only after preimage hashes still match, and removes only its exact temporary image tags.

The observer stores project-relative context paths and SHA-256 hashes, canonical project paths, Dockerfile and image identifiers, timestamps, coarse user-supplied tags and deployment target names, phase signals, layer/blob digests and sizes, and aggregate timings/counts. Optimization proofs additionally store candidate IDs, affected paths, preimage hashes, proof gates, and aggregate benchmark results. They deliberately exclude source contents, patch contents, Dockerfile instruction text, build/deployment/test/smoke commands, command output, progress logs, failure text, secret contents, environment values, and build-argument values. A Dockerfile's complete file hash and per-instruction hashes are fingerprints, not encryption; users with highly sensitive paths or instruction text should secure or remove the local cache.

Verified proof records expire after 30 days and are capped at 20 per project; unverified or failed records expire after seven days. Compact observation events may remain until the user removes the cache.

Build secrets and SSH mounts are passed directly to Buildx. They are visible transiently in the local process command line in the same way as a direct `docker buildx build` invocation, so prefer BuildKit secret mounts and file/environment sources over secret-valued build arguments.

Local state uses user-cache permissions where the operating system supports them, atomic snapshot replacement, a global state lock, and per-target build and deployment locks. Anyone who can modify that cache can alter recommendations or observation history.

Registry comparison reads the manifest that the configured Docker client resolves after a successful push. It compares declared compressed blob digests and sizes; it does not download layers, verify their contents, or measure actual network transfer.
