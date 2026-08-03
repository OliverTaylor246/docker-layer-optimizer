// Package optimize plans, verifies, and applies Docker build optimizations.
package optimize

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"

	"github.com/pmezard/go-difflib/difflib"
	"github.com/wendylabsinc/docker-layer-optimizer/internal/analyze"
	"github.com/wendylabsinc/docker-layer-optimizer/internal/contextfiles"
	"github.com/wendylabsinc/docker-layer-optimizer/internal/dockerfile"
	"github.com/wendylabsinc/docker-layer-optimizer/internal/version"
)

var protectedCommands = map[string]bool{"FROM": true, "CMD": true, "ENTRYPOINT": true, "USER": true, "EXPOSE": true, "HEALTHCHECK": true, "STOPSIGNAL": true, "VOLUME": true}
var dependencyTerms = []string{"apt-get install", "apk add", "dnf install", "yum install", "pip install", "pip3 install", "uv sync", "uv pip", "poetry install", "npm ci", "npm install", "pnpm install", "yarn install", "bundle install", "composer install", "go mod download", "cargo fetch", "swift package resolve"}

type Candidate struct {
	CandidateID      string   `json:"candidate_id"`
	Origin           string   `json:"origin"`
	Kind             string   `json:"kind"`
	Patch            string   `json:"patch"`
	AffectedPaths    []string `json:"affected_paths"`
	ProtectedChanges []string `json:"protected_changes"`
	Rationale        string   `json:"rationale"`
}
type Signal struct {
	MaxChangeLikelihood    float64 `json:"max_change_likelihood"`
	MaxExpectedRebuildCost float64 `json:"max_expected_rebuild_cost"`
}
type PlanResult struct {
	SchemaVersion      int              `json:"schema_version"`
	ToolVersion        string           `json:"tool_version"`
	Kind               string           `json:"kind"`
	Status             string           `json:"status"`
	ProjectRoot        string           `json:"project_root"`
	Dockerfile         string           `json:"dockerfile"`
	Evidence           analyze.Evidence `json:"evidence"`
	OptimizationSignal Signal           `json:"optimization_signal"`
	Candidate          *Candidate       `json:"candidate"`
	NextAction         string           `json:"next_action"`
}

func Plan(root, dockerfilePath, patch string) (PlanResult, error) {
	report, err := analyze.Project(root, dockerfilePath, 200)
	if err != nil {
		return PlanResult{}, err
	}
	var candidate *Candidate
	if patch != "" {
		value, err := CandidateFromPatch(root, dockerfilePath, patch, "agent")
		if err != nil {
			return PlanResult{}, err
		}
		candidate = &value
	} else {
		value, err := generateCandidate(root, dockerfilePath)
		if err != nil {
			return PlanResult{}, err
		}
		candidate = value
	}
	status := "no-candidate"
	next := "No conservative built-in rewrite was found; an agent may submit a unified diff with --candidate."
	if candidate != nil {
		status = "candidate"
		next = "Run `dlo optimize` with a verification contract to benchmark this candidate."
	}
	signal := Signal{}
	for _, layer := range report.Layers {
		if layer.ChangeLikelihood > signal.MaxChangeLikelihood {
			signal.MaxChangeLikelihood = layer.ChangeLikelihood
		}
		if layer.ExpectedRebuildCost > signal.MaxExpectedRebuildCost {
			signal.MaxExpectedRebuildCost = layer.ExpectedRebuildCost
		}
	}
	return PlanResult{1, version.Value, "optimization_plan", status, root, report.Dockerfile, report.Evidence, signal, candidate, next}, nil
}

func generateCandidate(root, dockerfilePath string) (*Candidate, error) {
	handle, err := os.Open(dockerfilePath)
	if err != nil {
		return nil, err
	}
	instructions, err := dockerfile.Parse(handle)
	handle.Close()
	if err != nil {
		return nil, err
	}
	tracked, err := gitTracked(root)
	if err != nil {
		return nil, err
	}
	files, err := contextfiles.Filter(root, dockerfilePath, tracked)
	if err != nil {
		return nil, err
	}
	manifests := analyze.ManifestFiles(files)
	if len(manifests) == 0 {
		return nil, nil
	}
	originalBytes, err := os.ReadFile(dockerfilePath)
	if err != nil {
		return nil, err
	}
	original := string(originalBytes)
	lines := splitLines(original)
	for index, instruction := range instructions {
		if index+1 >= len(instructions) {
			break
		}
		sources := instruction.ContextSources()
		if len(sources) != 1 || (sources[0] != "." && sources[0] != "./") || instruction.StartLine != instruction.EndLine {
			continue
		}
		dependency := instructions[index+1]
		if dependency.Stage != instruction.Stage || dependency.Command != "RUN" || !containsAny(strings.ToLower(dependency.Args), dependencyTerms) {
			continue
		}
		destination := instruction.ContextDestination()
		if destination == "" {
			continue
		}
		if destination == "." || destination == "./" {
			destination = "./"
		} else {
			destination = strings.TrimSuffix(destination, "/") + "/"
		}
		values := make([]string, 0, len(manifests)+1)
		for _, manifest := range manifests {
			encoded, _ := json.Marshal(manifest)
			values = append(values, string(encoded))
		}
		encodedDest, _ := json.Marshal(destination)
		values = append(values, string(encodedDest))
		manifestCopy := "COPY [" + strings.Join(values, ", ") + "]\n"
		copyIndex := instruction.StartLine - 1
		dependencyEnd := dependency.EndLine
		replacement := append([]string(nil), lines[:copyIndex]...)
		replacement = append(replacement, manifestCopy)
		replacement = append(replacement, lines[copyIndex+1:dependencyEnd]...)
		replacement = append(replacement, lines[copyIndex])
		replacement = append(replacement, lines[dependencyEnd:]...)
		after := strings.Join(replacement, "")
		if after == original {
			continue
		}
		relative, err := filepath.Rel(root, dockerfilePath)
		if err != nil || strings.HasPrefix(relative, "..") {
			return nil, fmt.Errorf("optimization requires the Dockerfile to be inside the project root")
		}
		patch, err := unified(filepath.ToSlash(relative), original, after)
		if err != nil {
			return nil, err
		}
		candidate, err := CandidateFromPatch(root, dockerfilePath, patch, "builtin")
		if err != nil {
			return nil, err
		}
		candidate.Kind = "manifest-first"
		candidate.Rationale = fmt.Sprintf("Copy %d dependency manifest(s) before the dependency installation at line %d, then copy volatile source so source-only edits can reuse that work.", len(manifests), dependency.StartLine)
		return &candidate, nil
	}
	return nil, nil
}

func CandidateFromPatch(root, dockerfilePath, patch, origin string) (Candidate, error) {
	paths, err := AffectedPaths(patch)
	if err != nil {
		return Candidate{}, err
	}
	if err := ApplyPatch(root, patch, true); err != nil {
		return Candidate{}, fmt.Errorf("%w\n%s", err, patch)
	}
	digest := sha256.Sum256([]byte(patch))
	kind := "agent-patch"
	rationale := "Agent-supplied Docker build candidate; DLO has not inferred its semantic intent."
	if origin == "builtin" {
		kind = "manifest-first"
		rationale = "Move volatile source copying after dependency installation."
	}
	protected, err := protectedChanges(root, dockerfilePath, patch, paths)
	if err != nil {
		return Candidate{}, err
	}
	return Candidate{hex.EncodeToString(digest[:10]), origin, kind, patch, paths, protected, rationale}, nil
}
func AffectedPaths(patch string) ([]string, error) {
	seen := map[string]bool{}
	for _, line := range strings.Split(patch, "\n") {
		if !strings.HasPrefix(line, "+++ ") {
			continue
		}
		value := strings.TrimSpace(strings.Split(strings.TrimPrefix(line, "+++ "), "\t")[0])
		if value == "/dev/null" {
			continue
		}
		value = strings.TrimPrefix(value, "b/")
		if filepath.IsAbs(value) || value == "" || strings.Contains(filepath.ToSlash(value), "../") {
			return nil, fmt.Errorf("candidate patch contains an unsafe path: %s", value)
		}
		seen[filepath.ToSlash(value)] = true
	}
	if len(seen) == 0 {
		return nil, fmt.Errorf("candidate patch does not contain a supported unified diff")
	}
	result := make([]string, 0, len(seen))
	for value := range seen {
		result = append(result, value)
	}
	sort.Strings(result)
	return result, nil
}
func ApplyPatch(root, patch string, check bool) error {
	args := []string{"apply", "--whitespace=nowarn"}
	if check {
		args = append(args, "--check")
	}
	command := exec.Command("git", args...)
	command.Dir = root
	command.Stdin = strings.NewReader(patch)
	output, err := command.CombinedOutput()
	if err != nil {
		return fmt.Errorf("%s", strings.TrimSpace(string(output)))
	}
	return nil
}

func protectedChanges(root, dockerfilePath, patch string, paths []string) ([]string, error) {
	var changes []string
	var disallowed []string
	for _, value := range paths {
		if !dockerBuildPath(value) {
			disallowed = append(disallowed, value)
		}
	}
	if len(disallowed) > 0 {
		changes = append(changes, "outside-docker-build-scope:"+strings.Join(disallowed, ","))
	}
	relative, _ := filepath.Rel(root, dockerfilePath)
	relative = filepath.ToSlash(relative)
	found := false
	for _, value := range paths {
		found = found || value == relative
	}
	if !found {
		return changes, nil
	}
	temporary, err := os.MkdirTemp("", "dlo-protected-")
	if err != nil {
		return nil, err
	}
	defer os.RemoveAll(temporary)
	if err := copyTree(root, temporary); err != nil {
		return nil, err
	}
	if err := ApplyPatch(temporary, patch, false); err != nil {
		return nil, err
	}
	candidatePath := filepath.Join(temporary, filepath.FromSlash(relative))
	if _, err := os.Stat(candidatePath); err != nil {
		return append(changes, "dockerfile-removed"), nil
	}
	before, err := protectedSequence(dockerfilePath)
	if err != nil {
		return nil, err
	}
	after, err := protectedSequence(candidatePath)
	if err != nil {
		return nil, err
	}
	if !equalSequence(before, after) {
		changes = append(changes, "protected-dockerfile-semantics")
	}
	return changes, nil
}
func protectedSequence(file string) ([]string, error) {
	handle, err := os.Open(file)
	if err != nil {
		return nil, err
	}
	instructions, err := dockerfile.Parse(handle)
	handle.Close()
	if err != nil {
		return nil, err
	}
	var values []string
	for _, instruction := range instructions {
		if protectedCommands[instruction.Command] {
			values = append(values, instruction.Command+"\x00"+instruction.Args)
		}
	}
	return values, nil
}
func equalSequence(left, right []string) bool {
	if len(left) != len(right) {
		return false
	}
	for index := range left {
		if left[index] != right[index] {
			return false
		}
	}
	return true
}
func dockerBuildPath(value string) bool {
	name := filepath.Base(value)
	if name == "Dockerfile" || name == "Containerfile" || name == ".dockerignore" || strings.HasPrefix(name, "Dockerfile.") || strings.HasPrefix(name, "Containerfile.") || strings.HasSuffix(name, ".dockerignore") {
		return true
	}
	switch name {
	case "compose.yml", "compose.yaml", "docker-compose.yml", "docker-compose.yaml":
		return true
	}
	return false
}
func unified(relative, before, after string) (string, error) {
	diff := difflib.UnifiedDiff{A: splitLines(before), B: splitLines(after), FromFile: "a/" + relative, ToFile: "b/" + relative, Context: 3}
	return difflib.GetUnifiedDiffString(diff)
}
func splitLines(value string) []string {
	if value == "" {
		return nil
	}
	values := strings.SplitAfter(value, "\n")
	if values[len(values)-1] == "" {
		values = values[:len(values)-1]
	}
	return values
}
func gitTracked(root string) ([]string, error) {
	command := exec.Command("git", "-C", root, "ls-files", "-z")
	output, err := command.CombinedOutput()
	if err != nil {
		return nil, fmt.Errorf("%s", strings.TrimSpace(string(output)))
	}
	var values []string
	for _, value := range bytes.Split(output, []byte{0}) {
		if len(value) > 0 {
			values = append(values, filepath.ToSlash(string(value)))
		}
	}
	sort.Strings(values)
	return values, nil
}
func containsAny(value string, terms []string) bool {
	for _, term := range terms {
		if strings.Contains(value, term) {
			return true
		}
	}
	return false
}
func copyTree(source, destination string) error {
	if err := os.MkdirAll(destination, 0o755); err != nil {
		return err
	}
	return filepath.WalkDir(source, func(path string, entry os.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		relative, err := filepath.Rel(source, path)
		if err != nil {
			return err
		}
		if relative == "." {
			return nil
		}
		if entry.IsDir() && entry.Name() == ".git" {
			return filepath.SkipDir
		}
		target := filepath.Join(destination, relative)
		if entry.IsDir() {
			return os.MkdirAll(target, 0o755)
		}
		if entry.Type()&os.ModeSymlink != 0 {
			value, err := os.Readlink(path)
			if err != nil {
				return err
			}
			return os.Symlink(value, target)
		}
		info, err := entry.Info()
		if err != nil {
			return err
		}
		data, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		return os.WriteFile(target, data, info.Mode().Perm())
	})
}
