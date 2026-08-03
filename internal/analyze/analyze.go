// Package analyze scores Docker context instructions using repository history
// and privacy-safe local observations.
package analyze

import (
	"bytes"
	"encoding/json"
	"fmt"
	"math"
	"os"
	"os/exec"
	"path"
	"path/filepath"
	"sort"
	"strings"

	"github.com/wendylabsinc/docker-layer-optimizer/internal/contextfiles"
	"github.com/wendylabsinc/docker-layer-optimizer/internal/dockerfile"
	"github.com/wendylabsinc/docker-layer-optimizer/internal/state"
)

const commitHeader = "__DLO_COMMIT__"

var manifestPatterns = []string{
	"requirements*.txt", "pyproject.toml", "poetry.lock", "uv.lock", "Pipfile", "Pipfile.lock",
	"package.json", "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml",
	"go.mod", "go.sum", "Cargo.toml", "Cargo.lock", "Package.swift", "Package.resolved",
	"Gemfile", "Gemfile.lock", "composer.json", "composer.lock", "pom.xml", "build.gradle*",
}

var dependencyTerms = []string{
	"apt-get install", "apk add", "dnf install", "yum install", "pip install", "pip3 install",
	"uv sync", "uv pip", "poetry install", "npm ci", "npm install", "pnpm install", "yarn install",
	"bundle install", "composer install", "go mod download", "cargo fetch", "swift package resolve",
}

var buildTerms = []string{
	"cargo build", "go build", "swift build", "npm run build", "pnpm build", "yarn build",
	"gradle build", "mvn package", "make", "cmake --build", "dotnet publish",
}

type Evidence struct {
	Commits                        int                `json:"commits"`
	LocalObservations              int                `json:"local_observations"`
	MedianDurationSeconds          *float64           `json:"median_duration_seconds"`
	MedianBytesPushed              *int64             `json:"median_bytes_pushed"`
	MedianContextBytes             *int64             `json:"median_context_bytes"`
	MeasuredBuilds                 int                `json:"measured_builds"`
	MedianRebuiltSteps             *float64           `json:"median_rebuilt_steps"`
	MedianUnmatchedDiffIDs         *float64           `json:"median_unmatched_diff_ids"`
	MedianUnmatchedCompressedBytes *int64             `json:"median_unmatched_compressed_bytes"`
	MedianNonBuildOverheadSeconds  *float64           `json:"median_non_build_overhead_seconds"`
	MeasuredDeployments            int                `json:"measured_deployments"`
	MedianDeploymentSeconds        *float64           `json:"median_deployment_seconds"`
	MedianDeploymentPhases         map[string]float64 `json:"median_deployment_phases"`
	DominantDeploymentPhase        *string            `json:"dominant_deployment_phase"`
	DeploymentStatusCounts         map[string]int     `json:"deployment_status_counts"`
}

type Area struct {
	Area             string  `json:"area"`
	ChangeLikelihood float64 `json:"change_likelihood"`
	Files            int     `json:"files"`
}

type CochangePair struct {
	Areas      []string `json:"areas"`
	Similarity float64  `json:"similarity"`
	Commits    int      `json:"commits"`
}

type Layer struct {
	Line                int      `json:"line"`
	Stage               int      `json:"stage"`
	Instruction         string   `json:"instruction"`
	Sources             []string `json:"sources"`
	MatchedFiles        int      `json:"matched_files"`
	ChangeLikelihood    float64  `json:"change_likelihood"`
	GitLikelihood       float64  `json:"git_likelihood"`
	LocalLikelihood     *float64 `json:"local_likelihood"`
	DownstreamCostUnits float64  `json:"downstream_cost_units"`
	ExpectedRebuildCost float64  `json:"expected_rebuild_cost"`
}

type Recommendation struct {
	Priority string `json:"priority"`
	Line     *int   `json:"line"`
	Kind     string `json:"kind"`
	Message  string `json:"message"`
}

type Report struct {
	SchemaVersion   int              `json:"schema_version"`
	ProjectRoot     string           `json:"project_root"`
	Dockerfile      string           `json:"dockerfile"`
	Evidence        Evidence         `json:"evidence"`
	VolatileAreas   []Area           `json:"volatile_areas"`
	CochangePairs   []CochangePair   `json:"cochange_pairs"`
	Layers          []Layer          `json:"layers"`
	Recommendations []Recommendation `json:"recommendations"`
}

func Project(root, dockerfilePath string, commitLimit int) (Report, error) {
	absoluteRoot, err := filepath.Abs(root)
	if err != nil {
		return Report{}, err
	}
	handle, err := os.Open(dockerfilePath)
	if err != nil {
		return Report{}, err
	}
	instructions, err := dockerfile.Parse(handle)
	_ = handle.Close()
	if err != nil {
		return Report{}, err
	}
	files, err := trackedFiles(absoluteRoot)
	if err != nil {
		return Report{}, err
	}
	files, err = contextfiles.Filter(absoluteRoot, dockerfilePath, files)
	if err != nil {
		return Report{}, fmt.Errorf("parse .dockerignore: %w", err)
	}
	commits, err := commitHistory(absoluteRoot, commitLimit)
	if err != nil {
		return Report{}, err
	}
	store, err := state.Open(absoluteRoot)
	if err != nil {
		return Report{}, err
	}
	events, err := store.Load(500)
	if err != nil {
		return Report{}, err
	}
	eventSets := eventChangeSets(events)
	manifests := ManifestFiles(files)

	layers := make([]Layer, 0)
	for index, instruction := range instructions {
		sources := instruction.ContextSources()
		if sources == nil {
			continue
		}
		matched := map[string]bool{}
		for _, source := range sources {
			for file := range matchesSource(source, files) {
				matched[file] = true
			}
		}
		gitLikelihood := probability(commits, matched, 30)
		localLikelihood := probability(eventSets, matched, 20)
		likelihood := 0.0
		switch {
		case len(commits) > 0 && len(eventSets) > 0:
			likelihood = 0.7*gitLikelihood + 0.3*localLikelihood
		case len(commits) > 0:
			likelihood = gitLikelihood
		case len(eventSets) > 0:
			likelihood = localLikelihood
		}
		cost := downstreamCost(instructions, index)
		var local *float64
		if len(eventSets) > 0 {
			value := round(localLikelihood, 4)
			local = &value
		}
		layers = append(layers, Layer{
			Line: instruction.StartLine, Stage: instruction.Stage, Instruction: instruction.Original,
			Sources: sources, MatchedFiles: len(matched), ChangeLikelihood: round(likelihood, 4),
			GitLikelihood: round(gitLikelihood, 4), LocalLikelihood: local,
			DownstreamCostUnits: round(cost, 2), ExpectedRebuildCost: round(likelihood*cost, 3),
		})
	}

	recommendations := recommendationsFor(absoluteRoot, instructions, layers, manifests, observedMetrics(events))
	sort.Slice(layers, func(left, right int) bool {
		return layers[left].ExpectedRebuildCost > layers[right].ExpectedRebuildCost
	})
	relativeDockerfile, relErr := filepath.Rel(absoluteRoot, dockerfilePath)
	if relErr != nil || strings.HasPrefix(relativeDockerfile, "..") {
		relativeDockerfile = dockerfilePath
	}
	return Report{
		SchemaVersion: 3, ProjectRoot: absoluteRoot, Dockerfile: filepath.ToSlash(relativeDockerfile),
		Evidence:      observedMetricsWithCounts(events, len(commits)),
		VolatileAreas: areaStats(commits, files), CochangePairs: cochangePairs(commits),
		Layers: layers, Recommendations: recommendations,
	}, nil
}

func trackedFiles(root string) ([]string, error) {
	value, err := git(root, "ls-files", "-z")
	if err != nil {
		return nil, err
	}
	items := strings.Split(string(value), "\x00")
	result := make([]string, 0, len(items))
	for _, item := range items {
		if item != "" {
			result = append(result, filepath.ToSlash(item))
		}
	}
	sort.Strings(result)
	return result, nil
}

func commitHistory(root string, limit int) ([]map[string]bool, error) {
	if limit <= 0 {
		return nil, nil
	}
	value, err := git(root, "log", fmt.Sprintf("-n%d", limit), "--format="+commitHeader+"%H", "--name-only", "--no-renames", "--", ".")
	if err != nil {
		return nil, err
	}
	var result []map[string]bool
	var current map[string]bool
	for _, line := range strings.Split(string(value), "\n") {
		if strings.HasPrefix(line, commitHeader) {
			if current != nil {
				result = append(result, current)
			}
			current = map[string]bool{}
		} else if current != nil && strings.TrimSpace(line) != "" {
			current[filepath.ToSlash(strings.TrimSpace(line))] = true
		}
	}
	if current != nil {
		result = append(result, current)
	}
	return result, nil
}

func git(root string, args ...string) ([]byte, error) {
	command := exec.Command("git", append([]string{"-C", root}, args...)...)
	value, err := command.CombinedOutput()
	if err != nil {
		return nil, fmt.Errorf("git %s: %s", strings.Join(args, " "), strings.TrimSpace(string(value)))
	}
	return value, nil
}

func eventChangeSets(events []map[string]any) []map[string]bool {
	result := make([]map[string]bool, 0, len(events))
	for index := len(events) - 1; index >= 0; index-- {
		values, ok := events[index]["changed_paths"].([]any)
		if !ok {
			continue
		}
		set := map[string]bool{}
		for _, value := range values {
			if text, ok := value.(string); ok {
				set[text] = true
			}
		}
		result = append(result, set)
	}
	return result
}

func probability(changeSets []map[string]bool, matched map[string]bool, halfLife float64) float64 {
	if len(changeSets) == 0 || len(matched) == 0 {
		return 0
	}
	numerator, denominator := 0.0, 0.0
	for index, changed := range changeSets {
		weight := math.Pow(0.5, float64(index)/halfLife)
		denominator += weight
		for file := range matched {
			if changed[file] {
				numerator += weight
				break
			}
		}
	}
	return numerator / denominator
}

func matchesSource(source string, files []string) map[string]bool {
	normalized := strings.TrimPrefix(filepath.ToSlash(source), "./")
	result := map[string]bool{}
	if source == "." || source == "./" || normalized == "" {
		for _, file := range files {
			result[file] = true
		}
		return result
	}
	if strings.Contains(source, "$") {
		return result
	}
	for _, file := range files {
		if strings.ContainsAny(normalized, "*?[") {
			if matched, _ := path.Match(normalized, file); matched {
				result[file] = true
			}
		} else if file == normalized || strings.HasPrefix(file, strings.TrimSuffix(normalized, "/")+"/") {
			result[file] = true
		}
	}
	return result
}

func ManifestFiles(files []string) []string {
	var result []string
	for _, file := range files {
		base := path.Base(file)
		for _, pattern := range manifestPatterns {
			if matched, _ := path.Match(pattern, base); matched {
				result = append(result, file)
				break
			}
		}
	}
	sort.Strings(result)
	return result
}

func instructionCost(instruction dockerfile.Instruction) float64 {
	switch instruction.Command {
	case "RUN":
		lower := strings.ToLower(instruction.Args)
		if containsTerm(lower, dependencyTerms) {
			return 10
		}
		if containsTerm(lower, buildTerms) {
			return 8
		}
		return 3
	case "COPY", "ADD":
		return 1
	case "FROM", "ARG":
		return 0
	default:
		return 0.5
	}
}

func containsTerm(value string, terms []string) bool {
	for _, term := range terms {
		if strings.Contains(value, term) {
			return true
		}
	}
	return false
}

func downstreamCost(instructions []dockerfile.Instruction, index int) float64 {
	stage := instructions[index].Stage
	total := 0.0
	for _, instruction := range instructions[index:] {
		if instruction.Stage == stage {
			total += instructionCost(instruction)
		}
	}
	return total
}

func topLevel(file string) string {
	if before, _, ok := strings.Cut(file, "/"); ok {
		return before
	}
	return "(root files)"
}

func areaStats(commits []map[string]bool, files []string) []Area {
	groups := map[string]map[string]bool{}
	for _, file := range files {
		name := topLevel(file)
		if groups[name] == nil {
			groups[name] = map[string]bool{}
		}
		groups[name][file] = true
	}
	result := make([]Area, 0, len(groups))
	for name, values := range groups {
		result = append(result, Area{name, round(probability(commits, values, 30), 4), len(values)})
	}
	sort.Slice(result, func(i, j int) bool {
		if result[i].ChangeLikelihood == result[j].ChangeLikelihood {
			return result[i].Area < result[j].Area
		}
		return result[i].ChangeLikelihood > result[j].ChangeLikelihood
	})
	if len(result) > 12 {
		result = result[:12]
	}
	return result
}

func cochangePairs(commits []map[string]bool) []CochangePair {
	groupSets := make([]map[string]bool, 0, len(commits))
	names := map[string]bool{}
	for _, changed := range commits {
		groups := map[string]bool{}
		for file := range changed {
			groups[topLevel(file)] = true
			names[topLevel(file)] = true
		}
		groupSets = append(groupSets, groups)
	}
	ordered := make([]string, 0, len(names))
	for name := range names {
		ordered = append(ordered, name)
	}
	sort.Strings(ordered)
	var result []CochangePair
	for left := 0; left < len(ordered); left++ {
		for right := left + 1; right < len(ordered); right++ {
			both, either := 0, 0
			for _, groups := range groupSets {
				l, r := groups[ordered[left]], groups[ordered[right]]
				if l && r {
					both++
				}
				if l || r {
					either++
				}
			}
			if both >= 2 && either > 0 && float64(both)/float64(either) >= 0.45 {
				result = append(result, CochangePair{[]string{ordered[left], ordered[right]}, round(float64(both)/float64(either), 4), both})
			}
		}
	}
	sort.Slice(result, func(i, j int) bool {
		if result[i].Similarity == result[j].Similarity {
			return result[i].Commits > result[j].Commits
		}
		return result[i].Similarity > result[j].Similarity
	})
	if len(result) > 8 {
		result = result[:8]
	}
	return result
}

func recommendationsFor(root string, instructions []dockerfile.Instruction, layers []Layer, manifests []string, metrics Evidence) []Recommendation {
	var result []Recommendation
	for _, layer := range layers {
		broad := false
		for _, source := range layer.Sources {
			broad = broad || source == "." || source == "./"
		}
		if !broad {
			continue
		}
		instructionIndex := -1
		for index, instruction := range instructions {
			if instruction.StartLine == layer.Line {
				instructionIndex = index
				break
			}
		}
		if instructionIndex < 0 {
			continue
		}
		var dependency *dockerfile.Instruction
		for index := instructionIndex + 1; index < len(instructions); index++ {
			candidate := instructions[index]
			if candidate.Stage != instructions[instructionIndex].Stage {
				continue
			}
			if candidate.Command == "RUN" && containsTerm(strings.ToLower(candidate.Args), dependencyTerms) {
				dependency = &candidate
				break
			}
		}
		if dependency != nil && len(manifests) > 0 {
			line := layer.Line
			listed := manifests
			if len(listed) > 8 {
				listed = listed[:8]
			}
			result = append(result, Recommendation{"high", &line, "split-dependency-inputs", fmt.Sprintf("Split dependency manifests into an earlier COPY, run dependency installation at line %d, then copy volatile source. Candidate manifests: %s.", dependency.StartLine, strings.Join(listed, ", "))})
		}
		if _, err := os.Stat(filepath.Join(root, ".dockerignore")); os.IsNotExist(err) {
			line := layer.Line
			result = append(result, Recommendation{"medium", &line, "add-dockerignore", "Add a .dockerignore so generated files and local state do not enter the build context."})
		}
	}
	if metrics.DominantDeploymentPhase != nil {
		phase := *metrics.DominantDeploymentPhase
		if phase == "readiness" || phase == "replacement" {
			result = append(result, Recommendation{"medium", nil, "investigate-deploy-runtime", fmt.Sprintf("Observed deployments are dominated by %s. Dockerfile layer ordering cannot remove this time; inspect container lifecycle, health checks, startup, and service warmup.", phase)})
		} else if phase == "transfer" || phase == "unpack" {
			result = append(result, Recommendation{"medium", nil, "reduce-deployment-layer-churn", fmt.Sprintf("Observed deployments are dominated by %s. Prioritize reducing changed layer content, then verify matching DiffIDs or registry blobs with representative source edits.", phase)})
		} else if phase == "build" || phase == "export" {
			result = append(result, Recommendation{"info", nil, "measure-deploy-build-path", fmt.Sprintf("Observed deployments are dominated by %s. Use measured cache steps and layer identity to validate the static Dockerfile recommendations.", phase)})
		}
	}
	if len(result) == 0 {
		result = append(result, Recommendation{"info", nil, "measure", "No obvious static layer split was found. Run representative warm builds with `dlo build` to add measured evidence."})
	}
	return result
}

func observedMetrics(events []map[string]any) Evidence { return observedMetricsWithCounts(events, 0) }

func observedMetricsWithCounts(events []map[string]any, commits int) Evidence {
	metrics := Evidence{Commits: commits, LocalObservations: len(events), MedianDeploymentPhases: map[string]float64{}, DeploymentStatusCounts: map[string]int{}}
	var durations, deploymentDurations, rebuilt, unmatched, compressed, overhead []float64
	var pushed, contexts []int64
	phaseValues := map[string][]float64{}
	dominantCounts := map[string]int{}
	for _, event := range events {
		if value, ok := number(event["duration_seconds"]); ok {
			durations = append(durations, value)
		}
		if value, ok := integer(event["bytes_pushed"]); ok {
			pushed = append(pushed, value)
		}
		if value, ok := integer(event["context_bytes"]); ok {
			contexts = append(contexts, value)
		}
		if steps, ok := event["steps"].(map[string]any); ok {
			metrics.MeasuredBuilds++
			if value, ok := number(steps["rebuilt"]); ok {
				rebuilt = append(rebuilt, value)
			}
			if image, ok := event["image"].(map[string]any); ok {
				if baseline, _ := image["has_baseline"].(bool); baseline {
					value := image["unmatched_diff_ids"]
					if value == nil {
						value = image["new"]
					}
					if numeric, ok := number(value); ok {
						unmatched = append(unmatched, numeric)
					}
				}
			}
			if registry, ok := event["registry"].(map[string]any); ok {
				if baseline, _ := registry["has_baseline"].(bool); baseline {
					if value, ok := number(registry["unmatched_compressed_bytes"]); ok {
						compressed = append(compressed, value)
					}
				}
			}
			if value, ok := nestedNumber(event, "overhead", "non_build_seconds"); ok {
				overhead = append(overhead, value)
			}
		}
		if deployment, ok := event["deployment"].(map[string]any); ok {
			metrics.MeasuredDeployments++
			if value, ok := number(event["duration_seconds"]); ok {
				deploymentDurations = append(deploymentDurations, value)
			}
			status, _ := event["status"].(string)
			if status == "" {
				status = "unknown"
			}
			metrics.DeploymentStatusCounts[status]++
			if dominant, ok := deployment["dominant_phase"].(string); ok {
				dominantCounts[dominant]++
			}
			if phases, ok := deployment["phases"].(map[string]any); ok {
				for phase, raw := range phases {
					if value, ok := raw.(map[string]any); ok {
						observed, _ := value["observed"].(bool)
						if observed {
							if seconds, ok := number(value["duration_seconds"]); ok {
								phaseValues[phase] = append(phaseValues[phase], seconds)
							}
						}
					}
				}
			}
		}
	}
	metrics.MedianDurationSeconds = medianFloat(durations, 3)
	metrics.MedianBytesPushed = medianInt(pushed)
	metrics.MedianContextBytes = medianInt(contexts)
	metrics.MedianRebuiltSteps = medianFloat(rebuilt, 1)
	metrics.MedianUnmatchedDiffIDs = medianFloat(unmatched, 1)
	if value := medianFloat(compressed, 0); value != nil {
		converted := int64(*value)
		metrics.MedianUnmatchedCompressedBytes = &converted
	}
	metrics.MedianNonBuildOverheadSeconds = medianFloat(overhead, 6)
	metrics.MedianDeploymentSeconds = medianFloat(deploymentDurations, 3)
	for phase, values := range phaseValues {
		if value := medianFloat(values, 3); value != nil {
			metrics.MedianDeploymentPhases[phase] = *value
		}
	}
	if len(dominantCounts) > 0 {
		var names []string
		for name := range dominantCounts {
			names = append(names, name)
		}
		sort.Slice(names, func(i, j int) bool {
			if dominantCounts[names[i]] == dominantCounts[names[j]] {
				return names[i] < names[j]
			}
			return dominantCounts[names[i]] > dominantCounts[names[j]]
		})
		metrics.DominantDeploymentPhase = &names[0]
	}
	return metrics
}

func number(value any) (float64, bool) {
	switch item := value.(type) {
	case float64:
		return item, true
	case int:
		return float64(item), true
	case int64:
		return float64(item), true
	case json.Number:
		value, err := item.Float64()
		return value, err == nil
	}
	return 0, false
}
func integer(value any) (int64, bool) { numeric, ok := number(value); return int64(numeric), ok }
func nestedNumber(event map[string]any, parent, child string) (float64, bool) {
	value, ok := event[parent].(map[string]any)
	if !ok {
		return 0, false
	}
	return number(value[child])
}

func medianFloat(values []float64, precision int) *float64 {
	if len(values) == 0 {
		return nil
	}
	copyValues := append([]float64(nil), values...)
	sort.Float64s(copyValues)
	middle := len(copyValues) / 2
	value := copyValues[middle]
	if len(copyValues)%2 == 0 {
		value = (copyValues[middle-1] + copyValues[middle]) / 2
	}
	value = round(value, precision)
	return &value
}
func medianInt(values []int64) *int64 {
	if len(values) == 0 {
		return nil
	}
	copyValues := append([]int64(nil), values...)
	sort.Slice(copyValues, func(i, j int) bool { return copyValues[i] < copyValues[j] })
	middle := len(copyValues) / 2
	value := copyValues[middle]
	if len(copyValues)%2 == 0 {
		value = (copyValues[middle-1] + copyValues[middle]) / 2
	}
	return &value
}

func round(value float64, precision int) float64 {
	factor := math.Pow10(precision)
	return math.Round(value*factor) / factor
}

// MarshalStable is used by compatibility tests and CLI JSON output.
func MarshalStable(report Report) ([]byte, error) {
	var buffer bytes.Buffer
	encoder := json.NewEncoder(&buffer)
	encoder.SetIndent("", "  ")
	encoder.SetEscapeHTML(false)
	err := encoder.Encode(report)
	return buffer.Bytes(), err
}
