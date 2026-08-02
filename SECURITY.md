# Security and privacy

## Reporting

Please report vulnerabilities privately with GitHub Security Advisories for this repository. Do not include active credentials in an issue.

## Threat model

`dlo build` executes the project's Dockerfile through the local Docker Buildx installation. `dlo deploy -- COMMAND` executes the provided command directly without a shell. Both Dockerfiles and deployment commands are executable input and should be treated as untrusted code. The tool does not add isolation or protect against a malicious command, Docker daemon, builder, registry, repository, base image, or deployment target.

The observer stores project-relative context paths and SHA-256 hashes, canonical project paths, Dockerfile and image identifiers, timestamps, coarse user-supplied tags and deployment target names, phase signals, layer/blob digests and sizes, and aggregate timings/counts. It deliberately excludes source contents, Dockerfile instruction text, build and deployment commands, command output, progress logs, failure text, secret contents, environment values, and build-argument values from persisted events. A Dockerfile's complete file hash and per-instruction hashes are fingerprints, not encryption; users with highly sensitive instruction text should secure or remove the local cache.

Build secrets and SSH mounts are passed directly to Buildx. They are visible transiently in the local process command line in the same way as a direct `docker buildx build` invocation, so prefer BuildKit secret mounts and file/environment sources over secret-valued build arguments.

Local state uses user-cache permissions where the operating system supports them, atomic snapshot replacement, a global state lock, and per-target build and deployment locks. Anyone who can modify that cache can alter recommendations or observation history.

Registry comparison reads the manifest that the configured Docker client resolves after a successful push. It compares declared compressed blob digests and sizes; it does not download layers, verify their contents, or measure actual network transfer.
