// Package deploy profiles deployment phases without requiring deploy-provider APIs.
package deploy

import (
	"fmt"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
)

var Phases = []string{"build", "export", "transfer", "unpack", "replacement", "readiness"}

var wendyPatterns = map[string][]string{
	"build":       {`^Building service\b`, `^Building and pushing image\b`, `^Building image \(OCI layout\)\b`, `^\[apple-container\] starting build:`},
	"export":      {`exporting to oci image format`, `exporting layers`, `sending tarball`},
	"transfer":    {`^\[apple-container\] pushing image:`, `^Pushing image\b`, `^Pulling image on device`, `^Diffing \d+ layer\(s\) against device`, `^Reusing \d+ layer\(s\) already on device`},
	"unpack":      {`^Unpack plan:`, `^Layer \d+/\d+ (?:reused|applying|unpacked)`},
	"replacement": {`^Creating container for service`, `^Creating container\.\.\.`, `^Service .+ container created\.`, `^App group .+ (?:running|created)`, `^Application .+ running in detached mode\.`},
	"readiness":   {`^Waiting for .+ to be ready`, `^Ready\.$`, `readiness probe timed out`, `^App reachable at `},
}
var composePatterns = map[string][]string{
	"build": {`^\[\+\] Building\b`, `^#\d+\b`, `^\s*=> \[`, `^\s*Building\b`}, "export": {`exporting to image`, `exporting layers`, `naming to `}, "transfer": {`\b(?:Pushing|Pulling|Downloading|Downloaded)\b`}, "unpack": {`\b(?:Extracting|Extracted)\b`}, "replacement": {`\bContainer .+ (?:Creating|Created|Recreate|Recreated|Starting|Started|Stopping|Stopped)\b`}, "readiness": {`\bContainer .+ (?:Waiting|Healthy|healthy)\b`, `\bWaiting for .+ (?:healthy|ready)\b`},
}

var timingPattern = regexp.MustCompile(`(?i)^\[timing\]\s+(.+?)\s+(\d+(?:\.\d+)?)(ms|us|µs|s)$`)
var signals = map[string]*regexp.Regexp{"readiness-timeout": regexp.MustCompile(`(?i)readiness (?:probe )?timed out|timed out .*waiting for .+ready`), "build-failed": regexp.MustCompile(`(?i)\b(?:build|builder) (?:failed|failure)\b|\b(?:build|builder) error(?:ed)?(?:\s*:|\s*$)|\bfailed to (?:build|solve)\b`), "deployment-failed": regexp.MustCompile(`(?i)\b(?:deployment|deploy|container|service) (?:failed|failure)\b|\b(?:deployment|deploy|container|service) error(?:ed)?(?:\s*:|\s*$)|\bfailed to (?:deploy|create|start|replace)\b`)}

type Phase struct {
	DurationSeconds float64 `json:"duration_seconds"`
	Segments        int     `json:"segments"`
	Observed        bool    `json:"observed"`
}
type Result struct {
	Adapter             string           `json:"adapter"`
	PhaseSource         string           `json:"phase_source"`
	Phases              map[string]Phase `json:"phases"`
	DominantPhase       *string          `json:"dominant_phase"`
	ClassifiedSeconds   float64          `json:"classified_seconds"`
	UnclassifiedSeconds float64          `json:"unclassified_seconds"`
	Signals             []string         `json:"signals"`
	ExitCode            int              `json:"exit_code"`
}
type phasePattern struct {
	phase      string
	expression *regexp.Regexp
}
type Tracker struct {
	adapter        string
	patterns       []phasePattern
	started        time.Time
	current        string
	currentStarted time.Time
	durations      map[string]time.Duration
	segments       map[string]int
	foundSignals   map[string]bool
	reported       map[string]float64
}

func InferAdapter(command []string) string {
	if len(command) == 0 {
		return "generic"
	}
	executable := strings.ToLower(filepath.Base(command[0]))
	if strings.Contains(executable, "wendy") {
		return "wendy"
	}
	if strings.HasPrefix(executable, "docker-compose") {
		return "compose"
	}
	if (executable == "docker" || executable == "docker.exe") && len(command) > 1 && strings.ToLower(command[1]) == "compose" {
		return "compose"
	}
	return "generic"
}
func PrepareCommand(command []string, adapter string) []string {
	prepared := append([]string(nil), command...)
	if adapter != "wendy" {
		return prepared
	}
	run := -1
	for index := 1; index < len(prepared); index++ {
		if strings.ToLower(prepared[index]) == "run" {
			run = index
			break
		}
	}
	if run < 0 {
		return prepared
	}
	for _, part := range prepared[1:] {
		if part == "--chunking" || strings.HasPrefix(part, "--chunking=") {
			return prepared
		}
	}
	prepared = append(prepared, nil...)
	prepared = append(prepared[:run+1], append([]string{"--chunking", "force"}, prepared[run+1:]...)...)
	return prepared
}

func NewTracker(adapter string, custom map[string][]string) (*Tracker, error) {
	source := map[string][]string{}
	if adapter == "wendy" {
		source = wendyPatterns
	} else if adapter == "compose" {
		source = composePatterns
	}
	combined := map[string][]string{}
	for _, phase := range Phases {
		combined[phase] = append([]string(nil), source[phase]...)
		combined[phase] = append(combined[phase], custom[phase]...)
	}
	priority := []string{"readiness", "replacement", "unpack", "transfer", "export", "build"}
	tracker := &Tracker{adapter: adapter, durations: map[string]time.Duration{}, segments: map[string]int{}, foundSignals: map[string]bool{}, reported: map[string]float64{}}
	for _, phase := range priority {
		for _, pattern := range combined[phase] {
			compiled, err := regexp.Compile("(?i)" + pattern)
			if err != nil {
				return nil, fmt.Errorf("invalid phase marker regex for %s: %w", phase, err)
			}
			tracker.patterns = append(tracker.patterns, phasePattern{phase, compiled})
		}
	}
	return tracker, nil
}
func ParseMarkers(values []string) (map[string][]string, error) {
	result := map[string][]string{}
	valid := map[string]bool{}
	for _, phase := range Phases {
		valid[phase] = true
	}
	for _, value := range values {
		phase, pattern, ok := strings.Cut(value, "=")
		if !ok || !valid[phase] || pattern == "" {
			return nil, fmt.Errorf("phase marker must be PHASE=REGEX where PHASE is one of %s", strings.Join(Phases, ", "))
		}
		if _, err := regexp.Compile(pattern); err != nil {
			return nil, fmt.Errorf("invalid phase marker regex for %s: %w", phase, err)
		}
		result[phase] = append(result[phase], pattern)
	}
	return result, nil
}
func (tracker *Tracker) Start(now time.Time) { tracker.started = now }
func (tracker *Tracker) Feed(line string, now time.Time) string {
	if tracker.adapter == "wendy" {
		if match := timingPattern.FindStringSubmatch(line); match != nil {
			value, _ := strconv.ParseFloat(match[2], 64)
			switch strings.ToLower(match[3]) {
			case "ms":
				value /= 1e3
			case "us", "µs":
				value /= 1e6
			}
			label := strings.ToLower(strings.TrimSpace(match[1]))
			if label == "build (oci export)" {
				tracker.reported["build"] = value
			} else if label == "chunk+query+write" {
				tracker.reported["transfer"] = value
			} else if strings.HasPrefix(label, "runcontainer (assemble+create+start") {
				tracker.reported["runcontainer"] = value
			}
		}
	}
	for code, expression := range signals {
		if expression.MatchString(line) {
			tracker.foundSignals[code] = true
		}
	}
	phase := ""
	for _, candidate := range tracker.patterns {
		if candidate.expression.MatchString(line) {
			phase = candidate.phase
			break
		}
	}
	if phase == "" || phase == tracker.current {
		return phase
	}
	if tracker.current != "" && !tracker.currentStarted.IsZero() {
		tracker.durations[tracker.current] += now.Sub(tracker.currentStarted)
	}
	tracker.current = phase
	tracker.currentStarted = now
	tracker.segments[phase]++
	return phase
}
func (tracker *Tracker) Finish(now time.Time) Result {
	if tracker.current != "" && !tracker.currentStarted.IsZero() {
		tracker.durations[tracker.current] += now.Sub(tracker.currentStarted)
		tracker.currentStarted = now
	}
	total := now.Sub(tracker.started).Seconds()
	result := Result{Adapter: tracker.adapter, PhaseSource: "output-markers", Phases: map[string]Phase{}}
	for _, phase := range Phases {
		result.Phases[phase] = Phase{round(tracker.durations[phase].Seconds(), 3), tracker.segments[phase], tracker.segments[phase] > 0}
	}
	if len(tracker.reported) > 0 {
		result.PhaseSource = "wendy-timing+output-markers"
		for _, phase := range []string{"build", "transfer"} {
			if value, ok := tracker.reported[phase]; ok {
				current := result.Phases[phase]
				current.DurationSeconds = round(value, 3)
				if current.Segments < 1 {
					current.Segments = 1
				}
				current.Observed = true
				result.Phases[phase] = current
			}
		}
		if value, ok := tracker.reported["runcontainer"]; ok {
			readiness := 0.0
			if result.Phases["readiness"].Observed {
				readiness = result.Phases["readiness"].DurationSeconds
			}
			current := result.Phases["replacement"]
			current.DurationSeconds = round(maxFloat(0, value-readiness), 3)
			if current.Segments < 1 {
				current.Segments = 1
			}
			current.Observed = true
			result.Phases["replacement"] = current
		}
	}
	classified := 0.0
	dominant := ""
	dominantValue := -1.0
	for _, phase := range Phases {
		value := result.Phases[phase]
		classified += value.DurationSeconds
		if value.Observed && value.DurationSeconds > dominantValue {
			dominant = phase
			dominantValue = value.DurationSeconds
		}
	}
	result.ClassifiedSeconds = round(minFloat(total, classified), 3)
	result.UnclassifiedSeconds = round(maxFloat(0, total-classified), 3)
	if dominant != "" {
		result.DominantPhase = &dominant
	}
	for code := range tracker.foundSignals {
		result.Signals = append(result.Signals, code)
	}
	sort.Strings(result.Signals)
	return result
}
func round(value float64, precision int) float64 {
	format := "%." + strconv.Itoa(precision) + "f"
	parsed, _ := strconv.ParseFloat(fmt.Sprintf(format, value), 64)
	return parsed
}
func minFloat(a, b float64) float64 {
	if a < b {
		return a
	}
	return b
}
func maxFloat(a, b float64) float64 {
	if a > b {
		return a
	}
	return b
}
