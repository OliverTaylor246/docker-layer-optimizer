package build

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
	"regexp"
	"sort"
	"strings"
	"time"

	"github.com/gofrs/flock"
	"github.com/moby/patternmatcher"
	"github.com/wendylabsinc/docker-layer-optimizer/internal/contextfiles"
	"github.com/wendylabsinc/docker-layer-optimizer/internal/registry"
	"github.com/wendylabsinc/docker-layer-optimizer/internal/state"
)

type Options struct {
	Root, Dockerfile, Tag, Platform, Target, Builder, Network string
	BuildArgs, CacheFrom, CacheTo, Secrets, SSH, Labels       []string
	BuildContexts, Provenance, SBOM                           []string
	NoCache, Pull, Push, Quiet, JSON                          bool
	ProgressFormat                                            string
	Stdout, Stderr                                            io.Writer
}

type Result struct {
	Event             map[string]any
	ExitCode          int
	InspectionDetails string
}

type targetSnapshot struct {
	Context                map[string]string `json:"context,omitempty"`
	LayerDiffIDs           []string          `json:"layer_diff_ids,omitempty"`
	RegistryLayers         []registry.Layer  `json:"registry_layers,omitempty"`
	LastObservation        string            `json:"last_observation,omitempty"`
	LastSuccessfulImage    string            `json:"last_successful_image,omitempty"`
	LastSuccessfulRegistry string            `json:"last_successful_registry_image,omitempty"`
}
type snapshotFile struct {
	SchemaVersion int                       `json:"schema_version"`
	Targets       map[string]targetSnapshot `json:"targets"`
}

type imageInspection struct {
	ID           string   `json:"id,omitempty"`
	SizeBytes    int64    `json:"size_bytes,omitempty"`
	RepoDigests  []string `json:"repo_digests"`
	LayerDiffIDs []string `json:"layer_diff_ids"`
	LayerComparison
}

func Command(options Options, metadataFile, progressFormat string) []string {
	command := []string{"docker", "buildx", "build", "--progress=" + progressFormat, "--metadata-file", metadataFile, "--file", options.Dockerfile, "--tag", options.Tag}
	if options.Push {
		command = append(command, "--push")
	} else {
		command = append(command, "--load")
	}
	for _, pair := range []struct{ flag, value string }{{"--platform", options.Platform}, {"--target", options.Target}, {"--builder", options.Builder}, {"--network", options.Network}} {
		if pair.value != "" {
			command = append(command, pair.flag, pair.value)
		}
	}
	if options.NoCache {
		command = append(command, "--no-cache")
	}
	if options.Pull {
		command = append(command, "--pull")
	}
	for _, value := range options.BuildArgs {
		command = append(command, "--build-arg", value)
	}
	for _, pair := range []struct {
		flag   string
		values []string
	}{{"--cache-from", options.CacheFrom}, {"--cache-to", options.CacheTo}, {"--secret", options.Secrets}, {"--ssh", options.SSH}, {"--label", options.Labels}, {"--build-context", options.BuildContexts}, {"--provenance", options.Provenance}, {"--sbom", options.SBOM}} {
		for _, value := range pair.values {
			command = append(command, pair.flag, value)
		}
	}
	return append(command, options.Root)
}

func SnapshotContext(root, dockerfile string) (map[string]string, error) {
	patterns, err := contextfiles.Patterns(root, dockerfile)
	if err != nil {
		return nil, err
	}
	matcher, err := patternmatcher.New(patterns)
	if err != nil {
		return nil, err
	}
	snapshot := map[string]string{}
	err = filepath.WalkDir(root, func(file string, entry os.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		relative, err := filepath.Rel(root, file)
		if err != nil {
			return err
		}
		if relative == "." {
			return nil
		}
		normalized := filepath.ToSlash(relative)
		if entry.IsDir() {
			if entry.Name() == ".git" {
				return filepath.SkipDir
			}
			return nil
		}
		ignored, err := matcher.MatchesOrParentMatches(normalized)
		if err != nil {
			return err
		}
		if ignored {
			return nil
		}
		info, err := entry.Info()
		if err != nil {
			return err
		}
		var value []byte
		if info.Mode()&os.ModeSymlink != 0 {
			target, err := os.Readlink(file)
			if err != nil {
				return nil
			}
			value = []byte(target)
		} else if info.Mode().IsRegular() {
			value, err = os.ReadFile(file)
			if err != nil {
				return nil
			}
		} else {
			return nil
		}
		digest := sha256.Sum256(value)
		snapshot[normalized] = hex.EncodeToString(digest[:])
		return nil
	})
	return snapshot, err
}

func ChangedPaths(current, previous map[string]string) []string {
	values := map[string]bool{}
	for path, value := range current {
		if previous[path] != value {
			values[path] = true
		}
	}
	for path, value := range previous {
		if current[path] != value {
			values[path] = true
		}
	}
	result := make([]string, 0, len(values))
	for value := range values {
		result = append(result, value)
	}
	sort.Strings(result)
	return result
}

func Run(ctx context.Context, options Options) (Result, error) {
	if options.Stdout == nil {
		options.Stdout = io.Discard
	}
	if options.Stderr == nil {
		options.Stderr = io.Discard
	}
	if options.ProgressFormat == "" {
		options.ProgressFormat = "auto"
	}
	if options.Tag == "" {
		options.Tag = "dlo/" + safeSlug(filepath.Base(options.Root)) + ":latest"
	}
	if _, err := exec.LookPath("docker"); err != nil {
		return Result{}, fmt.Errorf("docker was not found; install Docker with Buildx or run `dlo analyze` only")
	}
	store, err := state.Open(options.Root)
	if err != nil {
		return Result{}, err
	}
	if err := os.MkdirAll(store.Dir, 0o700); err != nil {
		return Result{}, err
	}
	targetKey := targetKey(options)
	targetLock := flock.New(filepath.Join(store.Dir, "target-"+targetKey+".lock"))
	if err := targetLock.Lock(); err != nil {
		return Result{}, err
	}
	defer func() { _ = targetLock.Unlock() }()
	statePath := filepath.Join(store.Dir, "snapshot.json")
	snapshotState, err := loadSnapshot(statePath)
	if err != nil {
		return Result{}, err
	}
	previous, hasPrevious := snapshotState.Targets[targetKey]
	wrapperStarted := time.Now()
	snapshotStarted := time.Now()
	currentSnapshot, err := SnapshotContext(options.Root, options.Dockerfile)
	if err != nil {
		return Result{}, err
	}
	snapshotSeconds := time.Since(snapshotStarted).Seconds()
	temporary, err := os.MkdirTemp("", "dlo-build-")
	if err != nil {
		return Result{}, err
	}
	defer os.RemoveAll(temporary)
	metadataPath := filepath.Join(temporary, "metadata.json")
	format := options.ProgressFormat
	if format == "auto" {
		format = "rawjson"
	}
	buildStarted := time.Now()
	exitCode, progress, err := execute(ctx, options, metadataPath, format)
	if err != nil {
		return Result{}, err
	}
	if options.ProgressFormat == "auto" && format == "rawjson" && rawJSONUnsupported(progress) {
		exitCode, progress, err = execute(ctx, options, metadataPath, "plain")
		if err != nil {
			return Result{}, err
		}
	}
	buildSeconds := time.Since(buildStarted).Seconds()
	metadata := readMetadata(metadataPath)
	inspectionStarted := time.Now()
	var image *imageInspection
	var registryImage *registry.Inspection
	inspectionCode := ""
	inspectionDetail := ""
	if exitCode == 0 && !options.Push {
		value, inspectErr := inspectImage(options.Tag)
		if inspectErr != nil {
			inspectionCode = "local-image-inspection-failed"
			inspectionDetail = inspectErr.Error()
		} else {
			value.LayerComparison = CompareLayers(value.LayerDiffIDs, previous.LayerDiffIDs, hasPrevious && previous.LayerDiffIDs != nil)
			image = &value
		}
	}
	if exitCode == 0 && options.Push {
		value, inspectErr := registry.Inspect(options.Tag, options.Platform, previous.RegistryLayers, hasPrevious && previous.RegistryLayers != nil)
		if inspectErr != nil {
			inspectionCode = "registry-manifest-inspection-failed"
			inspectionDetail = inspectErr.Error()
		} else {
			registryImage = &value
		}
	}
	inspectionSeconds := time.Since(inspectionStarted).Seconds()
	timestamp := time.Now().UTC().Format(time.RFC3339Nano)
	dockerfileBytes, _ := os.ReadFile(options.Dockerfile)
	dockerfileDigest := sha256.Sum256(dockerfileBytes)
	relative, _ := filepath.Rel(options.Root, options.Dockerfile)
	event := map[string]any{"schema_version": 3, "timestamp": timestamp, "kind": "build", "status": map[bool]string{true: "success", false: "failure"}[exitCode == 0], "project_root": options.Root, "target_key": targetKey, "dockerfile": map[string]any{"path": filepath.ToSlash(relative), "sha256": hex.EncodeToString(dockerfileDigest[:])}, "tag": options.Tag, "platform": nullable(options.Platform), "target": nullable(options.Target), "builder": nullable(options.Builder), "output": map[bool]string{true: "push", false: "load"}[options.Push], "duration_seconds": round6(buildSeconds, 3), "context_bytes": nullableInt(progress.ContextBytes()), "steps": progress.Summary(), "image": image, "registry": registryImage, "inspection_error": nullable(inspectionCode), "changed_paths": ChangedPaths(currentSnapshot, previous.Context), "metadata": metadata, "overhead": map[string]any{"snapshot_seconds": round6(snapshotSeconds, 6), "inspection_seconds": round6(inspectionSeconds, 6), "non_build_seconds": round6(snapshotSeconds+inspectionSeconds, 6), "wrapper_seconds": round6(time.Since(wrapperStarted).Seconds(), 6)}}
	updated := previous
	updated.Context = currentSnapshot
	updated.LastObservation = timestamp
	if image != nil {
		updated.LayerDiffIDs = image.LayerDiffIDs
		updated.LastSuccessfulImage = timestamp
	}
	if registryImage != nil {
		updated.RegistryLayers = registryImage.Layers
		updated.LastSuccessfulRegistry = timestamp
	}
	snapshotState.SchemaVersion = 2
	snapshotState.Targets[targetKey] = updated
	if err := writeSnapshot(statePath, snapshotState); err != nil {
		return Result{}, err
	}
	if _, err := store.Append(event); err != nil {
		return Result{}, err
	}
	return Result{Event: event, ExitCode: exitCode, InspectionDetails: inspectionDetail}, nil
}

func execute(ctx context.Context, options Options, metadataPath, format string) (int, ProgressParser, error) {
	var parser ProgressParser
	if format == "rawjson" {
		parser = NewRawJSONParser()
	} else {
		parser = NewPlainParser()
	}
	args := Command(options, metadataPath, format)
	command := exec.CommandContext(ctx, args[0], args[1:]...)
	command.Dir = options.Root
	stdout, err := command.StdoutPipe()
	if err != nil {
		return 0, nil, err
	}
	command.Stderr = command.Stdout
	if err := command.Start(); err != nil {
		return 0, nil, err
	}
	scanner := bufio.NewScanner(stdout)
	scanner.Buffer(make([]byte, 64*1024), 16*1024*1024)
	for scanner.Scan() {
		for _, message := range parser.Feed(scanner.Text()) {
			if !options.Quiet && !options.JSON {
				fmt.Fprintln(options.Stdout, message)
			}
		}
	}
	if err := scanner.Err(); err != nil {
		return 0, nil, err
	}
	err = command.Wait()
	if err == nil {
		return 0, parser, nil
	}
	if exit, ok := err.(*exec.ExitError); ok {
		return exit.ExitCode(), parser, nil
	}
	return 1, parser, err
}
func rawJSONUnsupported(parser ProgressParser) bool {
	text := strings.ToLower(strings.Join(parser.InvalidLines(), "\n"))
	return parser.EventsSeen() == 0 && strings.Contains(text, "rawjson") && (strings.Contains(text, "unknown") || strings.Contains(text, "invalid") || strings.Contains(text, "unsupported"))
}

func inspectImage(tag string) (imageInspection, error) {
	command := exec.Command("docker", "image", "inspect", tag)
	output, err := command.CombinedOutput()
	if err != nil {
		return imageInspection{}, fmt.Errorf("%s", strings.TrimSpace(string(output)))
	}
	var values []struct {
		ID          string
		Size        int64
		RepoDigests []string
		RootFS      struct{ Layers []string }
	}
	if err := json.Unmarshal(output, &values); err != nil {
		return imageInspection{}, err
	}
	if len(values) == 0 {
		return imageInspection{}, fmt.Errorf("docker image inspect returned no image for %s", tag)
	}
	return imageInspection{ID: values[0].ID, SizeBytes: values[0].Size, RepoDigests: values[0].RepoDigests, LayerDiffIDs: values[0].RootFS.Layers}, nil
}
func readMetadata(path string) map[string]string {
	value, err := os.ReadFile(path)
	if err != nil {
		return map[string]string{}
	}
	all := map[string]any{}
	if json.Unmarshal(value, &all) != nil {
		return map[string]string{}
	}
	result := map[string]string{}
	for _, key := range []string{"containerimage.digest", "containerimage.config.digest"} {
		if value, ok := all[key].(string); ok {
			result[key] = value
		}
	}
	return result
}
func loadSnapshot(path string) (snapshotFile, error) {
	value := snapshotFile{SchemaVersion: 2, Targets: map[string]targetSnapshot{}}
	data, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		return value, nil
	}
	if err != nil {
		return value, err
	}
	if json.Unmarshal(data, &value) != nil || value.Targets == nil {
		return snapshotFile{SchemaVersion: 2, Targets: map[string]targetSnapshot{}}, nil
	}
	return value, nil
}
func writeSnapshot(path string, value snapshotFile) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	temporary, err := os.CreateTemp(filepath.Dir(path), "snapshot-*.json")
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
func targetKey(options Options) string {
	value, _ := json.Marshal(struct{ Root, Dockerfile, Tag, Platform, Target, Output string }{options.Root, options.Dockerfile, options.Tag, options.Platform, options.Target, map[bool]string{true: "push", false: "load"}[options.Push]})
	digest := sha256.Sum256(value)
	return hex.EncodeToString(digest[:12])
}

var unsafeSlug = regexp.MustCompile(`[^a-z0-9_.-]+`)

func safeSlug(value string) string {
	value = strings.Trim(unsafeSlug.ReplaceAllString(strings.ToLower(value), "-"), "-._")
	if value == "" {
		return "project"
	}
	return value
}
func nullable(value string) any {
	if value == "" {
		return nil
	}
	return value
}
func nullableInt(value int64) any {
	if value == 0 {
		return nil
	}
	return value
}
func round6(value float64, precision int) float64 {
	format := "%." + fmt.Sprint(precision) + "f"
	parsed, _ := strconvParseFloat(fmt.Sprintf(format, value))
	return parsed
}
func strconvParseFloat(value string) (float64, error) {
	var result float64
	_, err := fmt.Sscan(value, &result)
	return result, err
}

func Render(result Result) string {
	steps, _ := result.Event["steps"].(Summary)
	lines := []string{fmt.Sprintf("dlo: %d cached, %d rebuilt, %d resolved, %d failed, %d incomplete Dockerfile steps", steps.Cached, steps.Rebuilt, steps.Resolved, steps.Failed, steps.Incomplete)}
	if image, ok := result.Event["image"].(*imageInspection); ok && image != nil {
		baseline := " (baseline recorded)"
		if image.HasBaseline {
			baseline = " vs previous build"
		}
		lines = append(lines, fmt.Sprintf("dlo: %d unmatched, %d matching layer DiffIDs; %d changed chain positions%s", image.UnmatchedDiffIDs, image.MatchingDiffIDs, image.ChangedPositions, baseline))
	}
	if value, ok := result.Event["registry"].(*registry.Inspection); ok && value != nil {
		baseline := " (baseline recorded)"
		if value.HasBaseline {
			baseline = " vs previous push"
		}
		lines = append(lines, fmt.Sprintf("dlo: %d unmatched/%d matching compressed blobs; %d unmatched compressed bytes%s", value.UnmatchedBlobs, value.MatchingBlobs, value.UnmatchedCompressedBytes, baseline))
	}
	return strings.Join(lines, "\n")
}
