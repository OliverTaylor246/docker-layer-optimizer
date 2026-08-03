package optimize

import (
	"math"
	"sort"
)

type Settings struct {
	Trials                                                                                                                         int
	BudgetSeconds, MinRelativeImprovement, MinAbsoluteSeconds, MaxRelativeRegression, MaxAbsoluteRegressionSeconds, PaybackDeploys float64
	SourcePath                                                                                                                     string
	VerificationCommands                                                                                                           []string
	Platform, Target, Builder                                                                                                      string
	BuildArgs                                                                                                                      []string
}

func DefaultSettings() Settings {
	return Settings{Trials: 3, BudgetSeconds: 600, MinRelativeImprovement: .10, MinAbsoluteSeconds: .5, MaxRelativeRegression: .10, MaxAbsoluteRegressionSeconds: .5, PaybackDeploys: 20}
}

type BuildResult struct {
	ReturnCode                             int
	DurationSeconds                        float64
	CachedSteps, RebuiltSteps, FailedSteps int
	ErrorKind                              string
}
type Stats struct {
	Runs               int     `json:"runs"`
	MedianSeconds      float64 `json:"median_seconds"`
	P95Seconds         float64 `json:"p95_seconds"`
	MedianCachedSteps  float64 `json:"median_cached_steps"`
	MedianRebuiltSteps float64 `json:"median_rebuilt_steps"`
}
type SourceBenchmark struct {
	Control                    Stats   `json:"control"`
	Candidate                  Stats   `json:"candidate"`
	AbsoluteImprovementSeconds float64 `json:"absolute_improvement_seconds"`
	RelativeImprovement        float64 `json:"relative_improvement"`
}
type PairBenchmark struct {
	ControlSeconds   float64 `json:"control_seconds"`
	CandidateSeconds float64 `json:"candidate_seconds"`
}
type Benchmark struct {
	SourceChange              SourceBenchmark `json:"source_change"`
	NoOp                      PairBenchmark   `json:"no_op"`
	DependencyChange          *PairBenchmark  `json:"dependency_change"`
	VerificationSeconds       float64         `json:"verification_seconds"`
	EstimatedBreakEvenDeploys *float64        `json:"estimated_break_even_deploys"`
}
type Gates struct {
	BuildsPassed                 bool `json:"builds_passed"`
	VerificationContractPresent  bool `json:"verification_contract_present"`
	VerificationCommandsPassed   bool `json:"verification_commands_passed"`
	MedianImprovement            bool `json:"median_improvement"`
	P95NotRegressed              bool `json:"p95_not_regressed"`
	NoOpNotRegressed             bool `json:"no_op_not_regressed"`
	DependencyChangeNotRegressed bool `json:"dependency_change_not_regressed"`
	PaybackWithinLimit           bool `json:"payback_within_limit"`
	BudgetRespected              bool `json:"budget_respected"`
	ProtectedChangesAbsent       bool `json:"protected_changes_absent,omitempty"`
}

func Evaluate(control, candidate []BuildResult, noOp [2]BuildResult, dependency *[2]BuildResult, correctness []bool, settings Settings, verificationSeconds float64) (Benchmark, Gates) {
	controlStats, candidateStats := stats(control), stats(candidate)
	absolute := controlStats.MedianSeconds - candidateStats.MedianSeconds
	relative := 0.0
	if controlStats.MedianSeconds != 0 {
		relative = absolute / controlStats.MedianSeconds
	}
	payback := (*float64)(nil)
	if absolute > 0 {
		value := roundEval(verificationSeconds/absolute, 1)
		payback = &value
	}
	allBuilds := true
	for _, value := range append(append(append([]BuildResult{}, control...), candidate...), noOp[:]...) {
		allBuilds = allBuilds && value.ReturnCode == 0
	}
	if dependency != nil {
		allBuilds = allBuilds && dependency[0].ReturnCode == 0 && dependency[1].ReturnCode == 0
	}
	commandsPassed := len(correctness) > 0
	for _, value := range correctness {
		commandsPassed = commandsPassed && value
	}
	dependencyAllowed := false
	if dependency != nil {
		dependencyAllowed = regressionAllowed(dependency[0].DurationSeconds, dependency[1].DurationSeconds, settings)
	}
	gates := Gates{BuildsPassed: allBuilds, VerificationContractPresent: len(correctness) > 0, VerificationCommandsPassed: commandsPassed, MedianImprovement: absolute >= settings.MinAbsoluteSeconds && relative >= settings.MinRelativeImprovement, P95NotRegressed: regressionAllowed(controlStats.P95Seconds, candidateStats.P95Seconds, settings), NoOpNotRegressed: regressionAllowed(noOp[0].DurationSeconds, noOp[1].DurationSeconds, settings), DependencyChangeNotRegressed: dependencyAllowed, PaybackWithinLimit: payback != nil && *payback <= settings.PaybackDeploys, BudgetRespected: verificationSeconds <= settings.BudgetSeconds, ProtectedChangesAbsent: true}
	benchmark := Benchmark{SourceChange: SourceBenchmark{controlStats, candidateStats, roundEval(absolute, 3), roundEval(relative, 4)}, NoOp: PairBenchmark{noOp[0].DurationSeconds, noOp[1].DurationSeconds}, VerificationSeconds: roundEval(verificationSeconds, 3), EstimatedBreakEvenDeploys: payback}
	if dependency != nil {
		benchmark.DependencyChange = &PairBenchmark{dependency[0].DurationSeconds, dependency[1].DurationSeconds}
	}
	return benchmark, gates
}
func AllGates(value Gates) bool {
	return value.BuildsPassed && value.VerificationContractPresent && value.VerificationCommandsPassed && value.MedianImprovement && value.P95NotRegressed && value.NoOpNotRegressed && value.DependencyChangeNotRegressed && value.PaybackWithinLimit && value.BudgetRespected && value.ProtectedChangesAbsent
}
func regressionAllowed(control, candidate float64, settings Settings) bool {
	tolerance := math.Max(settings.MaxAbsoluteRegressionSeconds, control*settings.MaxRelativeRegression)
	return candidate <= control+tolerance
}
func stats(values []BuildResult) Stats {
	durations := make([]float64, 0, len(values))
	cached := make([]float64, 0, len(values))
	rebuilt := make([]float64, 0, len(values))
	for _, value := range values {
		durations = append(durations, value.DurationSeconds)
		cached = append(cached, float64(value.CachedSteps))
		rebuilt = append(rebuilt, float64(value.RebuiltSteps))
	}
	sort.Float64s(durations)
	sort.Float64s(cached)
	sort.Float64s(rebuilt)
	return Stats{len(values), roundEval(median(durations), 3), roundEval(nearestP95(durations), 3), roundEval(median(cached), 1), roundEval(median(rebuilt), 1)}
}
func median(values []float64) float64 {
	if len(values) == 0 {
		return 0
	}
	middle := len(values) / 2
	if len(values)%2 == 0 {
		return (values[middle-1] + values[middle]) / 2
	}
	return values[middle]
}
func nearestP95(values []float64) float64 {
	if len(values) == 0 {
		return 0
	}
	index := int(math.Ceil(.95*float64(len(values)))) - 1
	if index < 0 {
		index = 0
	}
	return values[index]
}
func roundEval(value float64, precision int) float64 {
	factor := math.Pow10(precision)
	return math.Round(value*factor) / factor
}
