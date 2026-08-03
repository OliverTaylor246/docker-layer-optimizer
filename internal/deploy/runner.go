package deploy

import (
	"bufio"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/gofrs/flock"
	buildobserver "github.com/wendylabsinc/docker-layer-optimizer/internal/build"
	"github.com/wendylabsinc/docker-layer-optimizer/internal/state"
)

type Options struct {
	Root, Dockerfile, Adapter, Target string
	Markers, Tags, Command            []string
	Quiet, JSON                       bool
	Stdout, Stderr                    io.Writer
}
type RunResult struct {
	Event    map[string]any
	ExitCode int
	Command  []string
}
type deployTarget struct {
	Context         map[string]string `json:"context,omitempty"`
	LastObservation string            `json:"last_observation,omitempty"`
}
type deploySnapshot struct {
	SchemaVersion int                     `json:"schema_version"`
	Targets       map[string]deployTarget `json:"targets"`
}

func Run(ctx context.Context, options Options) (RunResult, error) {
	if len(options.Command) > 0 && options.Command[0] == "--" {
		options.Command = options.Command[1:]
	}
	if len(options.Command) == 0 {
		return RunResult{}, fmt.Errorf("pass a deployment command after `--`")
	}
	if options.Stdout == nil {
		options.Stdout = io.Discard
	}
	if options.Stderr == nil {
		options.Stderr = io.Discard
	}
	adapter := options.Adapter
	if adapter == "" || adapter == "auto" {
		adapter = InferAdapter(options.Command)
	}
	command := PrepareCommand(options.Command, adapter)
	markers, err := ParseMarkers(options.Markers)
	if err != nil {
		return RunResult{}, err
	}
	tracker, err := NewTracker(adapter, markers)
	if err != nil {
		return RunResult{}, err
	}
	targetName := ""
	if values := cleanTags([]string{options.Target}); len(values) > 0 {
		targetName = values[0]
	}
	key := deployKey(options.Root, adapter, targetName)
	store, err := state.Open(options.Root)
	if err != nil {
		return RunResult{}, err
	}
	if err := os.MkdirAll(store.Dir, 0o700); err != nil {
		return RunResult{}, err
	}
	lock := flock.New(filepath.Join(store.Dir, "deploy-"+key+".lock"))
	if err := lock.Lock(); err != nil {
		return RunResult{}, err
	}
	defer func() { _ = lock.Unlock() }()
	statePath := filepath.Join(store.Dir, "deploy-snapshot.json")
	snapshotState, err := loadDeploySnapshot(statePath)
	if err != nil {
		return RunResult{}, err
	}
	previous := snapshotState.Targets[key]
	dockerfile := options.Dockerfile
	if dockerfile == "" {
		dockerfile = filepath.Join(options.Root, "Dockerfile")
	}
	snapshot, err := buildobserver.SnapshotContext(options.Root, dockerfile)
	if err != nil {
		return RunResult{}, err
	}
	started := time.Now()
	tracker.Start(started)
	exitCode, err := execute(ctx, command, options.Root, adapter, tracker, options)
	if err != nil {
		return RunResult{}, err
	}
	completed := time.Now()
	deployment := tracker.Finish(completed)
	deployment.ExitCode = exitCode
	status := "success"
	if exitCode != 0 {
		status = "failure"
	} else if len(deployment.Signals) > 0 {
		status = "partial"
	}
	timestamp := time.Now().UTC().Format(time.RFC3339Nano)
	event := map[string]any{"schema_version": 3, "timestamp": timestamp, "kind": "deploy", "status": status, "project_root": options.Root, "target_key": key, "target_name": nil, "commit": gitCommit(options.Root), "dockerfile": nil, "changed_paths": buildobserver.ChangedPaths(snapshot, previous.Context), "tags": cleanTags(options.Tags), "duration_seconds": rounded(completed.Sub(started).Seconds(), 3), "deployment": deployment}
	if targetName != "" {
		event["target_name"] = targetName
	}
	if info, statErr := os.Stat(options.Dockerfile); options.Dockerfile != "" && statErr == nil && info.Mode().IsRegular() {
		data, _ := os.ReadFile(options.Dockerfile)
		digest := sha256.Sum256(data)
		relative, _ := filepath.Rel(options.Root, options.Dockerfile)
		event["dockerfile"] = map[string]any{"path": filepath.ToSlash(relative), "sha256": hex.EncodeToString(digest[:])}
	}
	snapshotState.SchemaVersion = 2
	snapshotState.Targets[key] = deployTarget{Context: snapshot, LastObservation: timestamp}
	if err := writeDeploySnapshot(statePath, snapshotState); err != nil {
		return RunResult{}, err
	}
	if _, err := store.Append(event); err != nil {
		return RunResult{}, err
	}
	return RunResult{event, exitCode, command}, nil
}

func execute(ctx context.Context, command []string, root, adapter string, tracker *Tracker, options Options) (int, error) {
	if _, err := exec.LookPath(command[0]); err != nil {
		return 0, fmt.Errorf("deployment executable was not found: %s", command[0])
	}
	process := exec.CommandContext(ctx, command[0], command[1:]...)
	process.Dir = root
	if adapter == "wendy" {
		environment := append([]string(nil), os.Environ()...)
		found := false
		for _, value := range environment {
			found = found || strings.HasPrefix(value, "WENDY_TIMING=")
		}
		if !found {
			environment = append(environment, "WENDY_TIMING=1")
		}
		process.Env = environment
	}
	output, err := process.StdoutPipe()
	if err != nil {
		return 0, err
	}
	process.Stderr = process.Stdout
	if err := process.Start(); err != nil {
		return 0, err
	}
	scanner := bufio.NewScanner(output)
	scanner.Buffer(make([]byte, 64*1024), 16*1024*1024)
	for scanner.Scan() {
		line := scanner.Text()
		tracker.Feed(strings.TrimRight(line, "\r\n"), time.Now())
		if !options.Quiet && !options.JSON {
			fmt.Fprintln(options.Stdout, line)
		}
	}
	if err := scanner.Err(); err != nil {
		return 0, err
	}
	err = process.Wait()
	if err == nil {
		return 0, nil
	}
	if exit, ok := err.(*exec.ExitError); ok {
		return exit.ExitCode(), nil
	}
	return 1, err
}
func deployKey(root, adapter, target string) string {
	if target == "" {
		target = "default"
	}
	value, _ := json.Marshal(struct{ Root, Adapter, Target string }{root, adapter, target})
	digest := sha256.Sum256(value)
	return hex.EncodeToString(digest[:12])
}
func cleanTags(values []string) []string {
	seen := map[string]bool{}
	for _, value := range values {
		value = strings.ToLower(value)
		var output strings.Builder
		dash := false
		for _, char := range value {
			if char >= 'a' && char <= 'z' || char >= '0' && char <= '9' {
				output.WriteRune(char)
				dash = false
			} else if !dash {
				output.WriteByte('-')
				dash = true
			}
		}
		normalized := strings.Trim(output.String(), "-")
		if len(normalized) > 40 {
			normalized = normalized[:40]
		}
		if normalized != "" {
			seen[normalized] = true
		}
	}
	result := make([]string, 0, len(seen))
	for value := range seen {
		result = append(result, value)
	}
	sort.Strings(result)
	return result
}
func gitCommit(root string) any {
	command := exec.Command("git", "-C", root, "rev-parse", "HEAD")
	value, err := command.Output()
	if err != nil {
		return nil
	}
	return strings.TrimSpace(string(value))
}
func loadDeploySnapshot(path string) (deploySnapshot, error) {
	value := deploySnapshot{SchemaVersion: 2, Targets: map[string]deployTarget{}}
	data, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		return value, nil
	}
	if err != nil {
		return value, err
	}
	if json.Unmarshal(data, &value) != nil || value.Targets == nil {
		return deploySnapshot{SchemaVersion: 2, Targets: map[string]deployTarget{}}, nil
	}
	return value, nil
}
func writeDeploySnapshot(path string, value deploySnapshot) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	temporary, err := os.CreateTemp(filepath.Dir(path), "deploy-snapshot-*.json")
	if err != nil {
		return err
	}
	name := temporary.Name()
	defer os.Remove(name)
	encoder := json.NewEncoder(temporary)
	encoder.SetIndent("", "  ")
	if err := encoder.Encode(value); err != nil {
		temporary.Close()
		return err
	}
	if err := temporary.Chmod(0o600); err != nil {
		temporary.Close()
		return err
	}
	if err := temporary.Close(); err != nil {
		return err
	}
	return os.Rename(name, path)
}
func rounded(value float64, precision int) float64 {
	format := "%." + fmt.Sprint(precision) + "f"
	var result float64
	fmt.Sscan(fmt.Sprintf(format, value), &result)
	return result
}

func Render(event map[string]any) string {
	deployment, ok := event["deployment"].(Result)
	if !ok {
		return "dlo: deployment observation recorded"
	}
	duration, _ := event["duration_seconds"].(float64)
	rows := []string{fmt.Sprintf("dlo: deployment %s in %.3fs (%s markers)", event["status"], duration, deployment.Adapter)}
	for _, phase := range Phases {
		item := deployment.Phases[phase]
		if item.Observed {
			rows = append(rows, fmt.Sprintf("dlo: %-11s %.3fs across %d segment(s)", phase, item.DurationSeconds, item.Segments))
		}
	}
	if deployment.UnclassifiedSeconds != 0 {
		rows = append(rows, fmt.Sprintf("dlo: unclassified %.3fs", deployment.UnclassifiedSeconds))
	}
	if deployment.DominantPhase != nil {
		rows = append(rows, "dlo: dominant phase: "+*deployment.DominantPhase)
	}
	if len(deployment.Signals) > 0 {
		rows = append(rows, "dlo: signals: "+strings.Join(deployment.Signals, ", "))
	}
	return strings.Join(rows, "\n")
}
