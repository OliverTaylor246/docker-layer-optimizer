// Package cli defines the stable dlo command interface used by people and agents.
package cli

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/spf13/cobra"
	"github.com/wendylabsinc/docker-layer-optimizer/internal/analyze"
	buildobserver "github.com/wendylabsinc/docker-layer-optimizer/internal/build"
	deployobserver "github.com/wendylabsinc/docker-layer-optimizer/internal/deploy"
	"github.com/wendylabsinc/docker-layer-optimizer/internal/optimize"
	"github.com/wendylabsinc/docker-layer-optimizer/internal/state"
	"github.com/wendylabsinc/docker-layer-optimizer/internal/version"
)

type commonOptions struct {
	root       string
	dockerfile string
	json       bool
}

// Execute runs the CLI and returns a process exit code without terminating the
// process, making the command interface the same seam used by tests.
func Execute(args []string, stdout, stderr io.Writer) int {
	command := NewRootCommand(stdout, stderr)
	command.SetArgs(args)
	if err := command.Execute(); err != nil {
		var processExit *exitError
		if errors.As(err, &processExit) {
			return processExit.code
		}
		jsonOutput, _ := command.Flags().GetBool("json")
		if jsonOutput {
			_ = json.NewEncoder(stderr).Encode(map[string]any{"error": err.Error(), "exit_code": 2})
		} else {
			fmt.Fprintf(stderr, "error: %v\n", err)
		}
		return 2
	}
	return 0
}

func NewRootCommand(stdout, stderr io.Writer) *cobra.Command {
	root := &cobra.Command{
		Use:           "dlo",
		Short:         "History-aware Docker layer optimizer",
		Version:       version.Value,
		SilenceUsage:  true,
		SilenceErrors: true,
	}
	root.SetOut(stdout)
	root.SetErr(stderr)
	root.AddCommand(newAnalyzeCommand(stdout), newBuildCommand(stdout, stderr), newDeployCommand(stdout, stderr), newOptimizeCommand(stdout), newRecordCommand(stdout), newHistoryCommand(stdout))
	return root
}

func newOptimizeCommand(stdout io.Writer) *cobra.Command {
	options := commonOptions{}
	var candidatePath, applyApproved, sourcePath, platform, target, builder string
	var trials int
	var budget, minRelative, minAbsolute, maxRelativeRegression, maxAbsoluteRegression, payback float64
	var planOnly, force bool
	var tests, smokeTests, buildArgs []string
	command := &cobra.Command{
		Use:   "optimize",
		Short: "plan, prove, and apply faster Dockerfile layer layouts",
		RunE: func(command *cobra.Command, _ []string) error {
			root, dockerfile, err := resolveProject(options.root, options.dockerfile)
			if err != nil {
				return err
			}
			patch := ""
			if candidatePath != "" {
				data, readErr := os.ReadFile(candidatePath)
				if readErr != nil {
					return fmt.Errorf("read candidate patch: %w", readErr)
				}
				patch = string(data)
			}
			overrides := optimize.SettingsOverrides{VerificationCommands: append(append([]string{}, tests...), smokeTests...), BuildArgs: append([]string{}, buildArgs...)}
			if command.Flags().Changed("trials") {
				overrides.Trials = &trials
			}
			if command.Flags().Changed("budget") {
				overrides.BudgetSeconds = &budget
			}
			if command.Flags().Changed("min-relative-improvement") {
				overrides.MinRelativeImprovement = &minRelative
			}
			if command.Flags().Changed("min-absolute-improvement") {
				overrides.MinAbsoluteSeconds = &minAbsolute
			}
			if command.Flags().Changed("max-relative-regression") {
				overrides.MaxRelativeRegression = &maxRelativeRegression
			}
			if command.Flags().Changed("max-absolute-regression") {
				overrides.MaxAbsoluteRegressionSeconds = &maxAbsoluteRegression
			}
			if command.Flags().Changed("payback-deploys") {
				overrides.PaybackDeploys = &payback
			}
			if command.Flags().Changed("source-path") {
				overrides.SourcePath = &sourcePath
			}
			if command.Flags().Changed("platform") {
				overrides.Platform = &platform
			}
			if command.Flags().Changed("target") {
				overrides.Target = &target
			}
			if command.Flags().Changed("builder") {
				overrides.Builder = &builder
			}
			outcome, err := optimize.RunOptimization(context.Background(), optimize.RunOptions{Root: root, Dockerfile: dockerfile, Patch: patch, ApplyApproved: applyApproved, PlanOnly: planOnly, Force: force, Overrides: overrides})
			if err != nil {
				return err
			}
			if options.json {
				encoder := json.NewEncoder(stdout)
				encoder.SetIndent("", "  ")
				if err := encoder.Encode(outcome.Result); err != nil {
					return err
				}
			} else {
				fmt.Fprintln(stdout, optimize.Render(outcome.Result))
			}
			if outcome.Code != 0 {
				return &exitError{code: outcome.Code, message: "optimization was not applied"}
			}
			return nil
		},
	}
	addCommonFlags(command, &options)
	command.Flags().BoolVar(&planOnly, "plan", false, "generate a candidate without benchmarking or applying it")
	command.Flags().StringVar(&candidatePath, "candidate", "", "unified diff proposed by an agent")
	command.Flags().StringVar(&applyApproved, "apply-approved", "", "apply the exact planned candidate ID without a performance proof")
	command.Flags().IntVar(&trials, "trials", 3, "paired source-edit trials")
	command.Flags().Float64Var(&budget, "budget", 600, "maximum verification seconds")
	command.Flags().Float64Var(&minRelative, "min-relative-improvement", .10, "required median relative improvement")
	command.Flags().Float64Var(&minAbsolute, "min-absolute-improvement", .5, "required median absolute improvement in seconds")
	command.Flags().Float64Var(&maxRelativeRegression, "max-relative-regression", .10, "allowed relative p95 and control regression")
	command.Flags().Float64Var(&maxAbsoluteRegression, "max-absolute-regression", .5, "allowed absolute p95 and control regression in seconds")
	command.Flags().Float64Var(&payback, "payback-deploys", 20, "maximum estimated deployments to repay verification")
	command.Flags().BoolVar(&force, "force", false, "benchmark despite the history-based payback precheck")
	command.Flags().StringVar(&sourcePath, "source-path", "", "representative source file to mutate")
	command.Flags().StringSliceVar(&tests, "test", nil, "correctness command required before auto-apply")
	command.Flags().StringSliceVar(&smokeTests, "smoke-test", nil, "runtime smoke command required before auto-apply")
	command.Flags().StringVar(&platform, "platform", "", "target platform")
	command.Flags().StringVar(&target, "target", "", "Dockerfile target")
	command.Flags().StringVar(&builder, "builder", "", "Buildx builder")
	command.Flags().StringSliceVar(&buildArgs, "build-arg", nil, "build argument")
	return command
}

func newDeployCommand(stdout, stderr io.Writer) *cobra.Command {
	options := commonOptions{}
	deployOptions := deployobserver.Options{Adapter: "auto", Stdout: stdout, Stderr: stderr}
	command := &cobra.Command{Use: "deploy -- COMMAND", Short: "profile build, transfer, unpack, replacement, and readiness", DisableFlagParsing: false, Args: cobra.ArbitraryArgs, RunE: func(_ *cobra.Command, args []string) error {
		root, err := resolveRoot(options.root)
		if err != nil {
			return err
		}
		deployOptions.Root = root
		deployOptions.Dockerfile = ""
		if options.dockerfile != "" {
			_, dockerfile, resolveErr := resolveProject(root, options.dockerfile)
			if resolveErr != nil {
				return resolveErr
			}
			deployOptions.Dockerfile = dockerfile
		}
		deployOptions.JSON = options.json
		deployOptions.Command = args
		result, err := deployobserver.Run(context.Background(), deployOptions)
		if err != nil {
			return err
		}
		if options.json {
			encoder := json.NewEncoder(stdout)
			encoder.SetIndent("", "  ")
			if err := encoder.Encode(result.Event); err != nil {
				return err
			}
		} else {
			fmt.Fprintln(stdout, deployobserver.Render(result.Event))
		}
		if result.ExitCode != 0 {
			return &exitError{code: result.ExitCode, message: "deployment failed"}
		}
		return nil
	}}
	addCommonFlags(command, &options)
	command.Flags().StringVar(&deployOptions.Adapter, "adapter", "auto", "auto, wendy, compose, or generic")
	command.Flags().StringVar(&deployOptions.Target, "target", "", "coarse deployment target name")
	command.Flags().StringSliceVar(&deployOptions.Markers, "phase-marker", nil, "PHASE=REGEX custom marker")
	command.Flags().StringSliceVar(&deployOptions.Tags, "tag", nil, "privacy-safe deployment tag")
	command.Flags().BoolVar(&deployOptions.Quiet, "quiet", false, "hide deployment output")
	return command
}

func newBuildCommand(stdout, stderr io.Writer) *cobra.Command {
	options := commonOptions{}
	buildOptions := buildobserver.Options{ProgressFormat: "auto", Stdout: stdout, Stderr: stderr}
	command := &cobra.Command{
		Use:   "build",
		Short: "run BuildKit and measure cache and image-layer changes",
		RunE: func(_ *cobra.Command, _ []string) error {
			root, dockerfile, err := resolveProject(options.root, options.dockerfile)
			if err != nil {
				return err
			}
			buildOptions.Root, buildOptions.Dockerfile = root, dockerfile
			buildOptions.JSON = options.json
			result, err := buildobserver.Run(context.Background(), buildOptions)
			if err != nil {
				return err
			}
			if options.json {
				encoder := json.NewEncoder(stdout)
				encoder.SetIndent("", "  ")
				if err := encoder.Encode(result.Event); err != nil {
					return err
				}
			} else {
				fmt.Fprintln(stdout, buildobserver.Render(result))
				if result.InspectionDetails != "" {
					fmt.Fprintf(stderr, "dlo: build recorded, but image inspection failed: %s\n", result.InspectionDetails)
				}
			}
			if result.ExitCode != 0 {
				return &exitError{code: result.ExitCode, message: "Docker build failed"}
			}
			return nil
		},
	}
	addCommonFlags(command, &options)
	command.Flags().StringVarP(&buildOptions.Tag, "tag", "t", "", "image tag")
	command.Flags().StringVar(&buildOptions.Platform, "platform", "", "target platform")
	command.Flags().StringVar(&buildOptions.Target, "target", "", "Dockerfile target")
	command.Flags().StringVar(&buildOptions.Builder, "builder", "", "Buildx builder")
	command.Flags().StringSliceVar(&buildOptions.BuildArgs, "build-arg", nil, "build argument")
	command.Flags().BoolVar(&buildOptions.NoCache, "no-cache", false, "disable build cache")
	command.Flags().BoolVar(&buildOptions.Pull, "pull", false, "always pull base images")
	command.Flags().StringSliceVar(&buildOptions.CacheFrom, "cache-from", nil, "external cache source")
	command.Flags().StringSliceVar(&buildOptions.CacheTo, "cache-to", nil, "external cache destination")
	command.Flags().StringSliceVar(&buildOptions.Secrets, "secret", nil, "BuildKit secret")
	command.Flags().StringSliceVar(&buildOptions.SSH, "ssh", nil, "BuildKit SSH forwarding")
	command.Flags().StringSliceVar(&buildOptions.Labels, "label", nil, "image label")
	command.Flags().StringSliceVar(&buildOptions.BuildContexts, "build-context", nil, "named build context")
	command.Flags().StringSliceVar(&buildOptions.Provenance, "provenance", nil, "provenance setting")
	command.Flags().StringSliceVar(&buildOptions.SBOM, "sbom", nil, "SBOM setting")
	command.Flags().StringVar(&buildOptions.Network, "network", "", "build network")
	command.Flags().BoolVar(&buildOptions.Push, "push", false, "push and compare compressed OCI registry blobs")
	command.Flags().StringVar(&buildOptions.ProgressFormat, "progress-format", "auto", "auto, rawjson, or plain")
	command.Flags().BoolVar(&buildOptions.Quiet, "quiet", false, "hide Docker progress")
	return command
}

type exitError struct {
	code    int
	message string
}

func (err *exitError) Error() string { return err.message }

func addCommonFlags(command *cobra.Command, options *commonOptions) {
	command.Flags().StringVar(&options.root, "root", ".", "project root")
	command.Flags().StringVar(&options.dockerfile, "dockerfile", "", "Dockerfile or Containerfile path")
	command.Flags().BoolVar(&options.json, "json", false, "emit machine-readable JSON")
}

func newAnalyzeCommand(stdout io.Writer) *cobra.Command {
	options := commonOptions{}
	commits := 200
	command := &cobra.Command{
		Use:   "analyze",
		Short: "score Docker context layers",
		RunE: func(command *cobra.Command, _ []string) error {
			root, dockerfile, err := resolveProject(options.root, options.dockerfile)
			if err != nil {
				return err
			}
			report, err := analyze.Project(root, dockerfile, commits)
			if err != nil {
				return err
			}
			if options.json {
				encoder := json.NewEncoder(stdout)
				encoder.SetIndent("", "  ")
				encoder.SetEscapeHTML(false)
				return encoder.Encode(report)
			}
			_, err = fmt.Fprintln(stdout, renderAnalysis(report))
			return err
		},
	}
	addCommonFlags(command, &options)
	command.Flags().IntVar(&commits, "commits", 200, "maximum Git commits to inspect")
	return command
}

func newRecordCommand(stdout io.Writer) *cobra.Command {
	options := commonOptions{}
	var kind, status string
	var duration float64
	var bytesPushed, invalidatedFrom int64
	var tags, changed []string
	var fromGit bool
	command := &cobra.Command{
		Use:   "record",
		Short: "append a privacy-safe local observation",
		RunE: func(command *cobra.Command, _ []string) error {
			root, err := resolveRoot(options.root)
			if err != nil {
				return err
			}
			paths := append([]string(nil), changed...)
			if fromGit {
				values, changeErr := currentChanges(root)
				if changeErr != nil {
					return changeErr
				}
				paths = append(paths, values...)
			}
			normalized, err := normalizeChangedPaths(root, paths)
			if err != nil {
				return err
			}
			event := map[string]any{
				"schema_version": 3, "timestamp": time.Now().UTC().Format(time.RFC3339Nano),
				"kind": kind, "status": status, "project_root": root,
				"commit": gitText(root, "rev-parse", "HEAD"), "changed_paths": normalized,
				"tags": cleanTags(tags), "duration_seconds": nil, "bytes_pushed": nil,
				"invalidated_from": nil, "dockerfile": nil,
			}
			if command.Flags().Changed("duration") {
				event["duration_seconds"] = duration
			}
			if command.Flags().Changed("bytes-pushed") {
				event["bytes_pushed"] = bytesPushed
			}
			if command.Flags().Changed("invalidated-from") {
				event["invalidated_from"] = invalidatedFrom
			}
			if _, dockerfile, resolveErr := resolveProject(root, options.dockerfile); resolveErr == nil {
				value, readErr := os.ReadFile(dockerfile)
				if readErr == nil {
					digest := sha256.Sum256(value)
					relative, _ := filepath.Rel(root, dockerfile)
					event["dockerfile"] = map[string]any{"path": filepath.ToSlash(relative), "sha256": hex.EncodeToString(digest[:])}
				}
			}
			store, err := state.Open(root)
			if err != nil {
				return err
			}
			path, err := store.Append(event)
			if err != nil {
				return err
			}
			result := map[string]any{"recorded": true, "state_file": path, "event": event}
			if options.json {
				encoder := json.NewEncoder(stdout)
				encoder.SetIndent("", "  ")
				return encoder.Encode(result)
			}
			_, err = fmt.Fprintf(stdout, "Recorded observation in %s\n", path)
			return err
		},
	}
	addCommonFlags(command, &options)
	command.Flags().StringVar(&kind, "kind", "", "observation kind: task, build, or deploy")
	_ = command.MarkFlagRequired("kind")
	command.Flags().StringVar(&status, "status", "success", "success, failure, or partial")
	command.Flags().Float64Var(&duration, "duration", 0, "duration in seconds")
	command.Flags().Int64Var(&bytesPushed, "bytes-pushed", 0, "bytes pushed")
	command.Flags().Int64Var(&invalidatedFrom, "invalidated-from", 0, "first invalidated Dockerfile line")
	command.Flags().StringSliceVar(&tags, "tag", nil, "coarse privacy-safe tag")
	command.Flags().StringSliceVar(&changed, "changed", nil, "changed path")
	command.Flags().BoolVar(&fromGit, "from-git", false, "record current Git changes")
	return command
}

func newHistoryCommand(stdout io.Writer) *cobra.Command {
	options := commonOptions{}
	limit := 20
	command := &cobra.Command{
		Use:   "history",
		Short: "show locally recorded observations",
		RunE: func(_ *cobra.Command, _ []string) error {
			root, err := resolveRoot(options.root)
			if err != nil {
				return err
			}
			store, err := state.Open(root)
			if err != nil {
				return err
			}
			events, err := store.Load(max(limit, 0))
			if err != nil {
				return err
			}
			for left, right := 0, len(events)-1; left < right; left, right = left+1, right-1 {
				events[left], events[right] = events[right], events[left]
			}
			if options.json {
				encoder := json.NewEncoder(stdout)
				encoder.SetIndent("", "  ")
				return encoder.Encode(events)
			}
			_, err = fmt.Fprintln(stdout, renderHistory(events))
			return err
		},
	}
	addCommonFlags(command, &options)
	command.Flags().IntVar(&limit, "limit", 20, "maximum observations")
	return command
}

func resolveRoot(value string) (string, error) {
	root, err := filepath.Abs(value)
	if err != nil {
		return "", err
	}
	info, err := os.Stat(root)
	if err != nil || !info.IsDir() {
		return "", fmt.Errorf("project root is not a directory: %s", root)
	}
	return root, nil
}

func resolveProject(rootValue, dockerfileValue string) (string, string, error) {
	root, err := resolveRoot(rootValue)
	if err != nil {
		return "", "", err
	}
	if dockerfileValue != "" {
		value := dockerfileValue
		if !filepath.IsAbs(value) {
			value = filepath.Join(root, value)
		}
		value, err = filepath.Abs(value)
		if err != nil {
			return "", "", err
		}
		if info, statErr := os.Stat(value); statErr != nil || !info.Mode().IsRegular() {
			return "", "", fmt.Errorf("Dockerfile does not exist: %s", value)
		}
		return root, value, nil
	}
	for _, name := range []string{"Dockerfile", "Containerfile"} {
		value := filepath.Join(root, name)
		if info, statErr := os.Stat(value); statErr == nil && info.Mode().IsRegular() {
			return root, value, nil
		}
	}
	return "", "", fmt.Errorf("no Dockerfile or Containerfile found; pass --dockerfile")
}

func currentChanges(root string) ([]string, error) {
	values := map[string]bool{}
	for _, args := range [][]string{{"diff", "--name-only", "-z", "HEAD"}, {"ls-files", "--others", "--exclude-standard", "-z"}} {
		command := exec.Command("git", append([]string{"-C", root}, args...)...)
		output, err := command.Output()
		if err != nil {
			return nil, err
		}
		for _, item := range strings.Split(string(output), "\x00") {
			if item != "" {
				values[filepath.ToSlash(item)] = true
			}
		}
	}
	result := make([]string, 0, len(values))
	for value := range values {
		result = append(result, value)
	}
	sort.Strings(result)
	return result, nil
}

func normalizeChangedPaths(root string, values []string) ([]string, error) {
	seen := map[string]bool{}
	for _, value := range values {
		absolute := value
		if !filepath.IsAbs(absolute) {
			absolute = filepath.Join(root, value)
		}
		absolute, _ = filepath.Abs(absolute)
		relative, err := filepath.Rel(root, absolute)
		if err != nil || relative == ".." || strings.HasPrefix(relative, ".."+string(filepath.Separator)) {
			return nil, fmt.Errorf("changed path is outside project root: %s", value)
		}
		seen[filepath.ToSlash(relative)] = true
	}
	result := make([]string, 0, len(seen))
	for value := range seen {
		result = append(result, value)
	}
	sort.Strings(result)
	return result, nil
}

func gitText(root string, args ...string) any {
	command := exec.Command("git", append([]string{"-C", root}, args...)...)
	output, err := command.Output()
	if err != nil {
		return nil
	}
	value := strings.TrimSpace(string(output))
	if value == "" {
		return nil
	}
	return value
}

var tagUnsafe = regexp.MustCompile(`[^a-z0-9-]+`)

func cleanTags(values []string) []string {
	seen := map[string]bool{}
	for _, value := range values {
		normalized := strings.Trim(tagUnsafe.ReplaceAllString(strings.ToLower(value), "-"), "-")
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

func renderAnalysis(report analyze.Report) string {
	var output strings.Builder
	fmt.Fprintf(&output, "Docker layer analysis: %s\nEvidence: %d commits, %d local observations\n", report.Dockerfile, report.Evidence.Commits, report.Evidence.LocalObservations)
	output.WriteString("\nHighest-risk context layers:\n")
	if len(report.Layers) == 0 {
		output.WriteString("  (no build-context COPY or ADD instructions found)\n")
	}
	for _, layer := range report.Layers {
		fmt.Fprintf(&output, "  line %d: risk %.3f, change %.1f%%, downstream cost %.1f\n    %s\n", layer.Line, layer.ExpectedRebuildCost, layer.ChangeLikelihood*100, layer.DownstreamCostUnits, layer.Instruction)
	}
	output.WriteString("\nRecommendations:\n")
	for index, item := range report.Recommendations {
		fmt.Fprintf(&output, "  %d. [%s] %s\n", index+1, item.Priority, item.Message)
	}
	return strings.TrimRight(output.String(), "\n")
}

func renderHistory(events []map[string]any) string {
	if len(events) == 0 {
		return "No observations recorded."
	}
	var output strings.Builder
	for _, event := range events {
		timestamp := fmt.Sprint(event["timestamp"])
		if parsed, err := time.Parse(time.RFC3339Nano, timestamp); err == nil {
			timestamp = parsed.Format("2006-01-02 15:04:05")
		}
		kind := fmt.Sprint(event["kind"])
		status := fmt.Sprint(event["status"])
		details := []string{}
		if values, ok := event["changed_paths"].([]any); ok && len(values) > 0 {
			details = append(details, strconv.Itoa(len(values))+" changed paths")
		}
		if values, ok := event["tags"].([]any); ok && len(values) > 0 {
			tags := make([]string, 0, len(values))
			for _, value := range values {
				tags = append(tags, fmt.Sprint(value))
			}
			details = append(details, "tags "+strings.Join(tags, ","))
		}
		if len(details) == 0 {
			details = append(details, "no measurements")
		}
		fmt.Fprintf(&output, "%s  %-7s %-7s %s\n", timestamp, kind, status, strings.Join(details, "; "))
	}
	return strings.TrimRight(output.String(), "\n")
}
