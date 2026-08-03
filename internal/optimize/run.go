package optimize

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/wendylabsinc/docker-layer-optimizer/internal/state"
)

type RunOptions struct {
	Root, Dockerfile, Patch, ApplyApproved string
	PlanOnly, Force                        bool
	Overrides                              SettingsOverrides
}
type Outcome struct {
	Code   int
	Result map[string]any
}

func RunOptimization(ctx context.Context, options RunOptions) (Outcome, error) {
	plan, err := Plan(options.Root, options.Dockerfile, options.Patch)
	if err != nil {
		return Outcome{}, err
	}
	if options.PlanOnly || plan.Candidate == nil {
		return Outcome{0, toMap(plan)}, nil
	}
	candidate := *plan.Candidate
	if options.ApplyApproved != "" {
		if options.ApplyApproved != candidate.CandidateID {
			return Outcome{}, fmt.Errorf("--apply-approved must exactly match the planned candidate ID")
		}
		before := preimages(options.Root, candidate.AffectedPaths)
		if err := ApplyPatch(options.Root, candidate.Patch, true); err != nil {
			return Outcome{}, err
		}
		if err := assertPreimages(options.Root, before); err != nil {
			return Outcome{}, err
		}
		if err := ApplyPatch(options.Root, candidate.Patch, false); err != nil {
			return Outcome{}, err
		}
		result := toMap(plan)
		result["kind"] = "optimization_result"
		result["status"] = "approved-applied"
		result["applied"] = true
		result["candidate"] = candidateWithoutPatch(candidate)
		result["next_action"] = "Review the working-tree diff; commit it or use Git to restore it."
		return Outcome{0, result}, nil
	}
	settings, err := LoadSettings(options.Root, options.Overrides)
	if err != nil {
		return Outcome{}, err
	}
	precheck := PaybackPrecheck(plan, settings)
	result := toMap(plan)
	result["payback_precheck"] = precheck
	if precheck["decision"] == "skip" && !options.Force {
		result["kind"] = "optimization_result"
		result["status"] = "skipped-payback"
		result["applied"] = false
		result["candidate"] = candidateWithoutPatch(candidate)
		result["next_action"] = "Use --force to benchmark despite the estimated payback, or collect more representative history."
		return Outcome{3, result}, nil
	}
	verificationStarted := time.Now()
	verification, verifyErr := Verify(ctx, options.Root, options.Dockerfile, candidate, settings, nil, nil)
	if verifyErr != nil {
		verification = Verification{OperationID: randomID(), Benchmark: Benchmark{VerificationSeconds: roundEval(time.Since(verificationStarted).Seconds(), 3)}, Gates: Gates{}, Verified: false, Preimages: preimages(options.Root, candidate.AffectedPaths), FailureKind: failureKind(verifyErr)}
	}
	applied := false
	if verification.Verified {
		if err := assertPreimages(options.Root, verification.Preimages); err != nil {
			return Outcome{}, err
		}
		if err := ApplyPatch(options.Root, candidate.Patch, false); err != nil {
			return Outcome{}, err
		}
		applied = true
	}
	proof := sanitizeProof(options.Root, candidate, verification, applied)
	proofPath, err := writeProof(options.Root, proof)
	if err != nil {
		return Outcome{}, err
	}
	store, err := state.Open(options.Root)
	if err != nil {
		return Outcome{}, err
	}
	event := map[string]any{"schema_version": 3, "timestamp": proof["timestamp"], "kind": "optimize", "status": map[bool]string{true: "success", false: "partial"}[applied], "project_root": options.Root, "changed_paths": map[bool][]string{true: candidate.AffectedPaths, false: {}}[applied], "tags": []string{map[bool]string{true: "verified", false: "unverified"}[verification.Verified], candidate.Kind}, "duration_seconds": verification.Benchmark.VerificationSeconds}
	if _, err := store.Append(event); err != nil {
		return Outcome{}, err
	}
	verificationMap := toMap(verification)
	delete(verificationMap, "preimages")
	result["kind"] = "optimization_result"
	result["status"] = map[bool]string{true: "verified-applied", false: "rejected"}[applied]
	result["applied"] = applied
	result["verification"] = verificationMap
	result["proof_file"] = proofPath
	result["candidate"] = candidateWithoutPatch(candidate)
	if applied {
		result["next_action"] = "Review the working-tree diff; commit it or use Git to restore it."
	} else {
		result["next_action"] = "Inspect the failed proof gates, revise the candidate or verification contract, and plan again."
	}
	code := 3
	if applied {
		code = 0
	}
	return Outcome{code, result}, nil
}

func PaybackPrecheck(plan PlanResult, settings Settings) map[string]any {
	measured := plan.Evidence.MeasuredBuilds
	baseline := plan.Evidence.MedianDurationSeconds
	likelihood := plan.OptimizationSignal.MaxChangeLikelihood
	if measured < 3 || baseline == nil || likelihood <= 0 {
		return map[string]any{"decision": "insufficient-history", "estimated_verification_seconds": nil, "estimated_break_even_deploys": nil, "assumption": "Explicit optimization may proceed within the hard time budget."}
	}
	estimatedVerification := *baseline * float64(settings.Trials+3)
	estimatedSavings := *baseline * likelihood * .5
	var breakEven *float64
	if estimatedSavings > 0 {
		value := roundEval(estimatedVerification/estimatedSavings, 1)
		breakEven = &value
	}
	decision := "skip"
	if breakEven != nil && *breakEven <= settings.PaybackDeploys {
		decision = "run"
	}
	return map[string]any{"decision": decision, "estimated_verification_seconds": roundEval(estimatedVerification, 1), "estimated_break_even_deploys": breakEven, "assumption": "Half of the change-weighted historical median is avoidable; measured proof replaces this estimate."}
}
func candidateWithoutPatch(candidate Candidate) map[string]any {
	value := toMap(candidate)
	delete(value, "patch")
	return value
}
func toMap(value any) map[string]any {
	data, _ := json.Marshal(value)
	result := map[string]any{}
	_ = json.Unmarshal(data, &result)
	return result
}
func failureKind(err error) string {
	value := strings.ToLower(err.Error())
	switch {
	case strings.Contains(value, "budget"):
		return "budget-exhausted"
	case strings.Contains(value, "correctness"):
		return "correctness-command-failed"
	case strings.Contains(value, "docker-build"):
		return "docker-build-failed"
	default:
		return "verification-failed"
	}
}
func sanitizeProof(root string, candidate Candidate, verification Verification, applied bool) map[string]any {
	rootDigest := sha256.Sum256([]byte(root))
	return map[string]any{"schema_version": 1, "kind": "optimization_proof", "timestamp": time.Now().UTC().Format(time.RFC3339Nano), "project_root_hash": hex.EncodeToString(rootDigest[:]), "candidate_id": candidate.CandidateID, "operation_id": verification.OperationID, "candidate_origin": candidate.Origin, "candidate_kind": candidate.Kind, "affected_paths": candidate.AffectedPaths, "protected_changes": candidate.ProtectedChanges, "verified": verification.Verified, "applied": applied, "benchmark": verification.Benchmark, "gates": verification.Gates, "failure_kind": nullableProof(verification.FailureKind), "before_sha256": verification.Preimages}
}
func writeProof(root string, proof map[string]any) (string, error) {
	store, err := state.Open(root)
	if err != nil {
		return "", err
	}
	directory := filepath.Join(store.Dir, "proofs")
	if err := os.MkdirAll(directory, 0o700); err != nil {
		return "", err
	}
	timestamp := strings.NewReplacer(":", "-", "+", "-").Replace(fmt.Sprint(proof["timestamp"]))
	path := filepath.Join(directory, timestamp+"-"+fmt.Sprint(proof["candidate_id"])+".json")
	data, err := json.MarshalIndent(proof, "", "  ")
	if err != nil {
		return "", err
	}
	data = append(data, '\n')
	if err := os.WriteFile(path, data, 0o600); err != nil {
		return "", err
	}
	_ = pruneProofs(directory, time.Now().UTC())
	return path, nil
}
func pruneProofs(directory string, now time.Time) error {
	entries, err := os.ReadDir(directory)
	if err != nil {
		return err
	}
	type retained struct {
		path      string
		timestamp time.Time
	}
	var kept []retained
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".json") {
			continue
		}
		path := filepath.Join(directory, entry.Name())
		data, readErr := os.ReadFile(path)
		if readErr != nil {
			_ = os.Remove(path)
			continue
		}
		var value map[string]any
		if json.Unmarshal(data, &value) != nil {
			_ = os.Remove(path)
			continue
		}
		timestamp, parseErr := time.Parse(time.RFC3339Nano, fmt.Sprint(value["timestamp"]))
		if parseErr != nil {
			_ = os.Remove(path)
			continue
		}
		verified, _ := value["verified"].(bool)
		lifetime := 7 * 24 * time.Hour
		if verified {
			lifetime = 30 * 24 * time.Hour
		}
		if now.Sub(timestamp) > lifetime {
			_ = os.Remove(path)
		} else {
			kept = append(kept, retained{path, timestamp})
		}
	}
	sort.Slice(kept, func(i, j int) bool { return kept[i].timestamp.After(kept[j].timestamp) })
	if len(kept) > 20 {
		for _, value := range kept[20:] {
			_ = os.Remove(value.path)
		}
	}
	return nil
}
func nullableProof(value string) any {
	if value == "" {
		return nil
	}
	return value
}

func Render(result map[string]any) string {
	candidate, ok := result["candidate"].(map[string]any)
	if !ok || candidate == nil {
		return "DLO found no conservative built-in optimization candidate. An agent may submit one with --candidate."
	}
	lines := []string{fmt.Sprintf("DLO optimization %v: %v", result["status"], candidate["candidate_id"]), fmt.Sprintf("Candidate: %v (%v)", candidate["kind"], candidate["origin"]), "Affected paths: " + joinAny(candidate["affected_paths"]), fmt.Sprint(candidate["rationale"])}
	if result["kind"] == "optimization_plan" {
		if protected := joinAny(candidate["protected_changes"]); protected != "" {
			lines = append(lines, "Approval-only changes: "+protected)
		}
		lines = append(lines, "", fmt.Sprint(candidate["patch"]), "", fmt.Sprint(result["next_action"]))
		return strings.Join(lines, "\n")
	}
	if verification, ok := result["verification"].(map[string]any); ok {
		lines = append(lines, fmt.Sprintf("Verified: %v; applied: %v", verification["verified"], result["applied"]))
	}
	if proof := fmt.Sprint(result["proof_file"]); proof != "<nil>" && proof != "" {
		lines = append(lines, "Proof: "+proof)
	}
	lines = append(lines, fmt.Sprint(result["next_action"]))
	return strings.Join(lines, "\n")
}
func joinAny(value any) string {
	values, ok := value.([]any)
	if !ok {
		if stringsValue, ok := value.([]string); ok {
			return strings.Join(stringsValue, ", ")
		}
		return ""
	}
	result := make([]string, 0, len(values))
	for _, item := range values {
		result = append(result, fmt.Sprint(item))
	}
	return strings.Join(result, ", ")
}
