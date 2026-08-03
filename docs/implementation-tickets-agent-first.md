# Agent-first optimizer implementation tickets

## DLO-1: Versioned optimization plan

- Add `dlo optimize --plan --json`.
- Generate deterministic candidate IDs and a conservative manifest-first patch.
- Accept agent-supplied unified diffs.
- Report protected changes, affected paths, evidence depth, and next action.
- Never mutate or persist patch contents in plan mode.

Acceptance: unit tests prove deterministic plans, valid patches, and zero project
mutation.

## DLO-2: Disposable verification snapshots

- Copy the exact current tree without `.git` into private temporary directories.
- Apply the candidate only inside the candidate snapshot.
- Record preimage hashes for affected real files.
- Refuse final application if any preimage changes during verification.

Acceptance: dirty and untracked files are represented; stale candidates cannot
be applied; temporary snapshots are cleaned after success and failure.

## DLO-3: Paired benchmark and correctness gates

- Warm control and candidate tags.
- Perform at least three paired source-change builds.
- Measure median, nearest-rank p95, step reuse, elapsed verification cost, and
  projected break-even count.
- Run `.dlo.yml` or explicit verification commands.
- Enforce the 10 percent plus 0.5 second threshold, p95, time budget, payback,
  correctness, and protected-change gates.

Acceptance: a fake build adapter covers every gate; a real-Docker integration
covers a known manifest-first improvement.

## DLO-4: Safe application and expiring proof records

- Auto-apply only a fully verified candidate.
- Keep the applied change as an ordinary working-tree diff.
- Persist a privacy-safe proof without patch, logs, commands, or source content.
- Retain at most 20 successful proofs younger than 30 days and failed proofs for
  seven days.

Acceptance: proof privacy and pruning tests pass; a changed preimage blocks
application.

## DLO-5: Agent documentation and beta release

- Update the skill, README, security contract, changelog, schemas, and examples.
- Add JSON-schema coverage for optimization observations.
- Validate Linux/macOS/Windows unit behavior, package installation, and real
  Docker behavior.
- Publish a GitHub prerelease after CI succeeds; PyPI remains gated by the
  repository's trusted-publishing setting.

## Deferred tickets

- DLO-6: CI regression budgets and check/propose workflow.
- DLO-7: Isolated Compose and Wendy canary adapters.
- DLO-8: Sanitized team-profile export/import.
- DLO-9: Kubernetes and hosted-builder adapters.
