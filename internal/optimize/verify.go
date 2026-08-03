package optimize

import (
	"bufio"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"time"

	"github.com/wendylabsinc/docker-layer-optimizer/internal/analyze"
	buildobserver "github.com/wendylabsinc/docker-layer-optimizer/internal/build"
	"github.com/wendylabsinc/docker-layer-optimizer/internal/state"
)

type BuildRunner func(context.Context, string, string, string, Settings, time.Time) (BuildResult, error)
type CommandRunner func(context.Context, string, string, map[string]string, time.Time) (bool, error)

type Verification struct {
	OperationID string             `json:"operation_id"`
	Benchmark   Benchmark          `json:"benchmark"`
	Gates       Gates              `json:"gates"`
	Verified    bool               `json:"verified"`
	Preimages   map[string]*string `json:"preimages,omitempty"`
	FailureKind string             `json:"failure_kind,omitempty"`
}

var textComments = map[string]string{
	".c": "//", ".cc": "//", ".cpp": "//", ".css": "/*", ".go": "//", ".h": "//",
	".hpp": "//", ".java": "//", ".js": "//", ".jsx": "//", ".kt": "//", ".mjs": "//",
	".md": "<!--", ".markdown": "<!--", ".php": "//", ".py": "#", ".rb": "#", ".rs": "//",
	".sh": "#", ".swift": "//", ".ts": "//", ".tsx": "//",
}
var manifestComments = map[string]string{".toml": "#", ".txt": "#", ".yaml": "#", ".yml": "#"}

func Verify(ctx context.Context, root, dockerfilePath string, candidate Candidate, settings Settings, runner BuildRunner, commandRunner CommandRunner) (Verification, error) {
	if runner == nil {
		runner = runDockerBuild
	}
	if commandRunner == nil {
		commandRunner = runCommand
	}
	started := time.Now()
	operationID := randomID()
	deadline := started.Add(time.Duration(settings.BudgetSeconds * float64(time.Second)))
	relative, err := filepath.Rel(root, dockerfilePath)
	if err != nil || strings.HasPrefix(relative, "..") {
		return Verification{}, fmt.Errorf("optimization requires the Dockerfile to be inside the project root")
	}
	sourcePath, err := safeSourcePath(root, settings.SourcePath)
	if err != nil {
		return Verification{}, err
	}
	if sourcePath == "" {
		return Verification{}, fmt.Errorf("no supported representative source file was found; set benchmark.source_path in .dlo.yml")
	}
	dependencyPath, err := safeManifestPath(root)
	if err != nil {
		return Verification{}, err
	}
	if dependencyPath == "" {
		return Verification{}, fmt.Errorf("no dependency manifest was found for the required negative-control build")
	}
	preimages := preimages(root, candidate.AffectedPaths)
	tagBase := "dlo-verify/" + safeProjectSlug(filepath.Base(root)) + "-" + candidate.CandidateID
	tags := []string{tagBase + ":control", tagBase + ":candidate"}
	store, err := state.Open(root)
	if err != nil {
		return Verification{}, err
	}
	snapshotParent := filepath.Join(store.Dir, "snapshots")
	if err := os.MkdirAll(snapshotParent, 0o700); err != nil {
		return Verification{}, err
	}
	temporary, err := os.MkdirTemp(snapshotParent, "dlo-optimize-")
	if err != nil {
		return Verification{}, err
	}
	defer os.RemoveAll(temporary)
	defer exec.Command("docker", "image", "rm", "-f", tags[0], tags[1]).Run()
	controlRoot, candidateRoot := filepath.Join(temporary, "control"), filepath.Join(temporary, "candidate")
	if err := copyTree(root, controlRoot); err != nil {
		return Verification{}, err
	}
	if err := copyTree(root, candidateRoot); err != nil {
		return Verification{}, err
	}
	if err := ApplyPatch(candidateRoot, candidate.Patch, false); err != nil {
		return Verification{}, err
	}
	controlDockerfile, candidateDockerfile := filepath.Join(controlRoot, relative), filepath.Join(candidateRoot, relative)

	buildOrFail := func(buildRoot, buildFile, tag string) (BuildResult, error) {
		if time.Now().After(deadline) {
			return BuildResult{}, fmt.Errorf("optimization budget exhausted")
		}
		result, err := runner(ctx, buildRoot, buildFile, tag, settings, deadline)
		if err != nil {
			return result, err
		}
		if result.ReturnCode != 0 {
			return result, fmt.Errorf("%s", defaultString(result.ErrorKind, "docker-build-failed"))
		}
		return result, nil
	}
	if _, err := buildOrFail(controlRoot, controlDockerfile, tags[0]); err != nil {
		return Verification{}, err
	}
	if _, err := buildOrFail(candidateRoot, candidateDockerfile, tags[1]); err != nil {
		return Verification{}, err
	}

	environment := map[string]string{"DLO_IMAGE_TAG": tags[1], "DLO_PROJECT_ROOT": candidateRoot}
	correctness := make([]bool, 0, len(settings.VerificationCommands))
	for _, command := range settings.VerificationCommands {
		passed, err := commandRunner(ctx, command, candidateRoot, environment, deadline)
		if err != nil {
			return Verification{}, err
		}
		correctness = append(correctness, passed)
		if !passed {
			return Verification{}, fmt.Errorf("correctness-command-failed")
		}
	}

	controlRuns, candidateRuns := []BuildResult{}, []BuildResult{}
	controlSource, candidateSource := filepath.Join(controlRoot, sourcePath), filepath.Join(candidateRoot, sourcePath)
	for trial := 0; trial < settings.Trials; trial++ {
		marker := fmt.Sprintf("source-%d-%s", trial, operationID)
		if err := mutate(controlSource, marker, false); err != nil {
			return Verification{}, err
		}
		if err := mutate(candidateSource, marker, false); err != nil {
			return Verification{}, err
		}
		type target struct {
			root, file, tag string
			values          *[]BuildResult
		}
		order := []target{{controlRoot, controlDockerfile, tags[0], &controlRuns}, {candidateRoot, candidateDockerfile, tags[1], &candidateRuns}}
		if trial%2 == 1 {
			order[0], order[1] = order[1], order[0]
		}
		for _, item := range order {
			result, err := buildOrFail(item.root, item.file, item.tag)
			if err != nil {
				return Verification{}, err
			}
			*item.values = append(*item.values, result)
		}
	}
	controlNoOp, err := buildOrFail(controlRoot, controlDockerfile, tags[0])
	if err != nil {
		return Verification{}, err
	}
	candidateNoOp, err := buildOrFail(candidateRoot, candidateDockerfile, tags[1])
	if err != nil {
		return Verification{}, err
	}
	if err := mutate(filepath.Join(controlRoot, dependencyPath), "dependency-"+operationID, true); err != nil {
		return Verification{}, err
	}
	if err := mutate(filepath.Join(candidateRoot, dependencyPath), "dependency-"+operationID, true); err != nil {
		return Verification{}, err
	}
	controlDependency, err := buildOrFail(controlRoot, controlDockerfile, tags[0])
	if err != nil {
		return Verification{}, err
	}
	candidateDependency, err := buildOrFail(candidateRoot, candidateDockerfile, tags[1])
	if err != nil {
		return Verification{}, err
	}
	dependency := [2]BuildResult{controlDependency, candidateDependency}
	benchmark, gates := Evaluate(controlRuns, candidateRuns, [2]BuildResult{controlNoOp, candidateNoOp}, &dependency, correctness, settings, time.Since(started).Seconds())
	gates.ProtectedChangesAbsent = len(candidate.ProtectedChanges) == 0
	verification := Verification{OperationID: operationID, Benchmark: benchmark, Gates: gates, Verified: AllGates(gates), Preimages: preimages}
	if err := assertPreimages(root, preimages); err != nil {
		return Verification{}, err
	}
	return verification, nil
}

func runDockerBuild(ctx context.Context, root, dockerfilePath, tag string, settings Settings, deadline time.Time) (BuildResult, error) {
	if _, err := exec.LookPath("docker"); err != nil {
		return BuildResult{}, fmt.Errorf("docker was not found; install Docker with Buildx before verification")
	}
	args := []string{"buildx", "build", "--progress=rawjson", "--load", "--file", dockerfilePath, "--tag", tag}
	for _, pair := range []struct{ flag, value string }{{"--platform", settings.Platform}, {"--target", settings.Target}, {"--builder", settings.Builder}} {
		if pair.value != "" {
			args = append(args, pair.flag, pair.value)
		}
	}
	for _, value := range settings.BuildArgs {
		args = append(args, "--build-arg", value)
	}
	args = append(args, root)
	buildContext, cancel := context.WithDeadline(ctx, deadline)
	defer cancel()
	command := exec.CommandContext(buildContext, "docker", args...)
	command.Dir = root
	output, err := command.StdoutPipe()
	if err != nil {
		return BuildResult{}, err
	}
	command.Stderr = command.Stdout
	started := time.Now()
	if err := command.Start(); err != nil {
		return BuildResult{}, err
	}
	parser := buildobserver.NewRawJSONParser()
	scanner := bufio.NewScanner(output)
	scanner.Buffer(make([]byte, 64*1024), 16*1024*1024)
	for scanner.Scan() {
		parser.Feed(scanner.Text())
	}
	if err := scanner.Err(); err != nil {
		return BuildResult{}, err
	}
	waitErr := command.Wait()
	if buildContext.Err() != nil {
		return BuildResult{}, fmt.Errorf("Docker build exceeded the optimization budget")
	}
	returnCode := 0
	if waitErr != nil {
		if exit, ok := waitErr.(*exec.ExitError); ok {
			returnCode = exit.ExitCode()
		} else {
			return BuildResult{}, waitErr
		}
	}
	summary := parser.Summary()
	kind := ""
	if returnCode != 0 {
		kind = "docker-build-failed"
	}
	return BuildResult{returnCode, roundEval(time.Since(started).Seconds(), 3), summary.Cached, summary.Rebuilt, summary.Failed, kind}, nil
}

func runCommand(ctx context.Context, command, root string, environment map[string]string, deadline time.Time) (bool, error) {
	commandContext, cancel := context.WithDeadline(ctx, deadline)
	defer cancel()
	var process *exec.Cmd
	if runtime.GOOS == "windows" {
		process = exec.CommandContext(commandContext, "cmd", "/C", command)
	} else {
		process = exec.CommandContext(commandContext, "/bin/sh", "-c", command)
	}
	process.Dir, process.Stdout, process.Stderr = root, io.Discard, io.Discard
	values := append([]string(nil), os.Environ()...)
	for key, value := range environment {
		values = append(values, key+"="+value)
	}
	process.Env = values
	err := process.Run()
	if commandContext.Err() != nil {
		return false, fmt.Errorf("verification command exceeded the optimization budget")
	}
	if err == nil {
		return true, nil
	}
	if _, ok := err.(*exec.ExitError); ok {
		return false, nil
	}
	return false, err
}

func safeSourcePath(root, configured string) (string, error) {
	if configured != "" {
		if filepath.IsAbs(configured) || strings.HasPrefix(filepath.Clean(configured), "..") {
			return "", fmt.Errorf("benchmark source_path must stay inside the project root")
		}
		info, err := os.Stat(filepath.Join(root, configured))
		if err != nil || !info.Mode().IsRegular() || textComments[strings.ToLower(filepath.Ext(configured))] == "" {
			return "", fmt.Errorf("benchmark source_path must name a supported text source file")
		}
		return filepath.ToSlash(configured), nil
	}
	tracked, err := gitTracked(root)
	if err != nil {
		return "", err
	}
	manifests := map[string]bool{}
	for _, value := range analyze.ManifestFiles(tracked) {
		manifests[value] = true
	}
	var candidates []string
	for _, value := range tracked {
		if !manifests[value] && textComments[strings.ToLower(filepath.Ext(value))] != "" && filepath.Base(value) != "Dockerfile" && filepath.Base(value) != "Containerfile" {
			candidates = append(candidates, value)
		}
	}
	sort.Slice(candidates, func(i, j int) bool {
		leftPreferred, rightPreferred := preferredName(filepath.Base(candidates[i])), preferredName(filepath.Base(candidates[j]))
		if leftPreferred != rightPreferred {
			return leftPreferred
		}
		leftDepth, rightDepth := strings.Count(candidates[i], "/"), strings.Count(candidates[j], "/")
		if leftDepth != rightDepth {
			return leftDepth < rightDepth
		}
		return candidates[i] < candidates[j]
	})
	if len(candidates) == 0 {
		return "", nil
	}
	return candidates[0], nil
}
func safeManifestPath(root string) (string, error) {
	tracked, err := gitTracked(root)
	if err != nil {
		return "", err
	}
	for _, value := range analyze.ManifestFiles(tracked) {
		if info, statErr := os.Stat(filepath.Join(root, value)); statErr == nil && info.Mode().IsRegular() {
			return value, nil
		}
	}
	return "", nil
}
func preferredName(value string) bool {
	for _, prefix := range []string{"app.", "main.", "server.", "index."} {
		if strings.HasPrefix(value, prefix) {
			return true
		}
	}
	return false
}
func mutate(path, marker string, manifest bool) error {
	suffix := strings.ToLower(filepath.Ext(path))
	addition := "\n"
	if !(manifest && (suffix == ".json" || suffix == ".lock")) {
		prefix := textComments[suffix]
		if manifest {
			prefix = manifestComments[suffix]
		}
		if prefix == "" {
			prefix = "#"
		}
		addition = "\n" + prefix + " dlo benchmark " + marker
		if prefix == "/*" {
			addition += " */"
		} else if prefix == "<!--" {
			addition += " -->"
		}
		addition += "\n"
	}
	handle, err := os.OpenFile(path, os.O_APPEND|os.O_WRONLY, 0)
	if err != nil {
		return err
	}
	_, writeErr := handle.WriteString(addition)
	closeErr := handle.Close()
	if writeErr != nil {
		return writeErr
	}
	return closeErr
}

func preimages(root string, paths []string) map[string]*string {
	values := map[string]*string{}
	for _, value := range paths {
		values[value] = fileHash(filepath.Join(root, value))
	}
	return values
}
func assertPreimages(root string, expected map[string]*string) error {
	var changed []string
	for path, digest := range expected {
		if !equalHash(fileHash(filepath.Join(root, path)), digest) {
			changed = append(changed, path)
		}
	}
	sort.Strings(changed)
	if len(changed) > 0 {
		return fmt.Errorf("candidate is stale; affected files changed during verification: %s", strings.Join(changed, ", "))
	}
	return nil
}
func fileHash(path string) *string {
	info, err := os.Lstat(path)
	if os.IsNotExist(err) || err != nil {
		return nil
	}
	var value []byte
	if info.Mode()&os.ModeSymlink != 0 {
		target, err := os.Readlink(path)
		if err != nil {
			return nil
		}
		value = []byte(target)
	} else if info.Mode().IsRegular() {
		value, err = os.ReadFile(path)
		if err != nil {
			return nil
		}
	} else {
		text := "non-regular"
		return &text
	}
	digest := sha256.Sum256(value)
	text := hex.EncodeToString(digest[:])
	return &text
}
func equalHash(left, right *string) bool {
	if left == nil || right == nil {
		return left == nil && right == nil
	}
	return *left == *right
}
func randomID() string {
	digest := sha256.Sum256([]byte(fmt.Sprintf("%d-%d", time.Now().UnixNano(), os.Getpid())))
	return hex.EncodeToString(digest[:8])
}
func safeProjectSlug(value string) string {
	value = strings.ToLower(value)
	var output strings.Builder
	for _, char := range value {
		if char >= 'a' && char <= 'z' || char >= '0' && char <= '9' || strings.ContainsRune("_.-", char) {
			output.WriteRune(char)
		} else {
			output.WriteByte('-')
		}
	}
	value = strings.Trim(output.String(), "-._")
	if value == "" {
		return "project"
	}
	return value
}
func defaultString(value, fallback string) string {
	if value == "" {
		return fallback
	}
	return value
}
