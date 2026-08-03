package optimize

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestPlanUsesBuildKitModelToGenerateManifestFirstCandidate(t *testing.T) {
	root := t.TempDir()
	gitOpt(t, root, "init", "-q")
	gitOpt(t, root, "config", "user.name", "Test")
	gitOpt(t, root, "config", "user.email", "test@example.com")
	writeOpt(t, root, "requirements.txt", "flask==3.0.0\n")
	writeOpt(t, root, "app.py", "print('hi')\n")
	writeOpt(t, root, "Dockerfile", "FROM python:3.12\nCOPY --link . /app\nRUN pip install -r /app/requirements.txt\nCMD [\"python\",\"/app/app.py\"]\n")
	gitOpt(t, root, "add", ".")
	gitOpt(t, root, "commit", "-qm", "initial")
	result, err := Plan(root, filepath.Join(root, "Dockerfile"), "")
	if err != nil {
		t.Fatal(err)
	}
	if result.Status != "candidate" || result.Candidate == nil {
		t.Fatalf("plan=%#v", result)
	}
	patch := result.Candidate.Patch
	if !strings.Contains(patch, `COPY ["requirements.txt", "/app/"]`) || strings.Count(patch, "COPY --link . /app") != 1 {
		t.Fatalf("unexpected patch:\n%s", patch)
	}
	if len(result.Candidate.ProtectedChanges) != 0 {
		t.Fatalf("protected changes=%#v", result.Candidate.ProtectedChanges)
	}
	if err := ApplyPatch(root, patch, true); err != nil {
		t.Fatalf("candidate does not apply: %v", err)
	}
}

func TestVerifyUsesPairedTrialsAndReturnsAutoApplyProof(t *testing.T) {
	root := t.TempDir()
	gitOpt(t, root, "init", "-q")
	gitOpt(t, root, "config", "user.name", "Test")
	gitOpt(t, root, "config", "user.email", "test@example.com")
	writeOpt(t, root, "requirements.txt", "flask==3.0.0\n")
	writeOpt(t, root, "app.py", "print('hi')\n")
	writeOpt(t, root, "Dockerfile", "FROM python:3.12\nCOPY . /app\nRUN pip install -r /app/requirements.txt\n")
	gitOpt(t, root, "add", ".")
	gitOpt(t, root, "commit", "-qm", "initial")
	plan, err := Plan(root, filepath.Join(root, "Dockerfile"), "")
	if err != nil || plan.Candidate == nil {
		t.Fatalf("plan=%#v err=%v", plan, err)
	}
	settings := DefaultSettings()
	settings.SourcePath = "app.py"
	settings.VerificationCommands = []string{"test"}
	calls := map[string]int{}
	runner := func(_ context.Context, buildRoot, _ string, _ string, _ Settings, _ time.Time) (BuildResult, error) {
		kind := filepath.Base(buildRoot)
		calls[kind]++
		index := calls[kind]
		duration := 1.0
		if index >= 2 && index <= 4 {
			if kind == "control" {
				duration = 10
			} else {
				duration = 2
			}
		}
		if index == 6 {
			duration = 10
		}
		return BuildResult{ReturnCode: 0, DurationSeconds: duration, CachedSteps: 5, RebuiltSteps: 1}, nil
	}
	verification, err := Verify(context.Background(), root, filepath.Join(root, "Dockerfile"), *plan.Candidate, settings, runner, func(context.Context, string, string, map[string]string, time.Time) (bool, error) { return true, nil })
	if err != nil {
		t.Fatal(err)
	}
	if !verification.Verified || !AllGates(verification.Gates) {
		t.Fatalf("verification=%#v", verification)
	}
	if calls["control"] != 6 || calls["candidate"] != 6 {
		t.Fatalf("calls=%#v", calls)
	}
}

func TestEvaluationRequiresImprovementCorrectnessAndNegativeControls(t *testing.T) {
	settings := DefaultSettings()
	settings.Trials = 3
	settings.VerificationCommands = []string{"go test ./..."}
	control := []BuildResult{{0, 10, 1, 5, 0, ""}, {0, 11, 1, 5, 0, ""}, {0, 9, 1, 5, 0, ""}}
	candidate := []BuildResult{{0, 2, 5, 1, 0, ""}, {0, 2.2, 5, 1, 0, ""}, {0, 1.8, 5, 1, 0, ""}}
	benchmark, gates := Evaluate(control, candidate, [2]BuildResult{{ReturnCode: 0, DurationSeconds: 1}, {ReturnCode: 0, DurationSeconds: 1}}, &[2]BuildResult{{ReturnCode: 0, DurationSeconds: 10}, {ReturnCode: 0, DurationSeconds: 10}}, []bool{true}, settings, 30)
	if benchmark.SourceChange.RelativeImprovement < 0.79 || !AllGates(gates) {
		t.Fatalf("benchmark=%#v gates=%#v", benchmark, gates)
	}
}

func writeOpt(t *testing.T, root, relative, value string) {
	t.Helper()
	path := filepath.Join(root, relative)
	if err := os.WriteFile(path, []byte(value), 0o644); err != nil {
		t.Fatal(err)
	}
}
func gitOpt(t *testing.T, root string, args ...string) {
	t.Helper()
	command := exec.Command("git", args...)
	command.Dir = root
	if output, err := command.CombinedOutput(); err != nil {
		t.Fatalf("git %v: %v: %s", args, err, output)
	}
}
