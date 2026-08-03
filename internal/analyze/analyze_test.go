package analyze

import (
	"os"
	"os/exec"
	"path/filepath"
	"testing"
)

func TestProjectFindsBroadCopyBeforeDependencyInstallAndHonorsDockerignore(t *testing.T) {
	root := t.TempDir()
	runGit(t, root, "init", "-q")
	runGit(t, root, "config", "user.name", "Test")
	runGit(t, root, "config", "user.email", "test@example.com")
	write(t, root, "requirements.txt", "flask==3.0.0\n")
	write(t, root, "app.py", "print('one')\n")
	write(t, root, "docs/guide.md", "ignored\n")
	write(t, root, ".dockerignore", "docs/\n")
	write(t, root, "Dockerfile", "FROM python:3.12-slim\nCOPY . /app\nRUN pip install -r /app/requirements.txt\n")
	runGit(t, root, "add", ".")
	runGit(t, root, "commit", "-qm", "initial")
	for index := 0; index < 3; index++ {
		write(t, root, "app.py", string(rune('a'+index))+"\n")
		runGit(t, root, "add", "app.py")
		runGit(t, root, "commit", "-qm", "source change")
	}

	report, err := Project(root, filepath.Join(root, "Dockerfile"), 100)
	if err != nil {
		t.Fatalf("Project: %v", err)
	}
	if report.Evidence.Commits != 4 {
		t.Fatalf("commits = %d, want 4", report.Evidence.Commits)
	}
	if len(report.Layers) != 1 || report.Layers[0].MatchedFiles != 2 {
		t.Fatalf("layers = %#v", report.Layers)
	}
	if report.Layers[0].ChangeLikelihood <= 0.5 {
		t.Fatalf("change likelihood = %f, want > 0.5", report.Layers[0].ChangeLikelihood)
	}
	found := false
	for _, recommendation := range report.Recommendations {
		found = found || recommendation.Kind == "split-dependency-inputs"
	}
	if !found {
		t.Fatalf("recommendations = %#v", report.Recommendations)
	}
	for _, area := range report.VolatileAreas {
		if area.Area == "docs" {
			t.Fatal("dockerignored docs leaked into volatility analysis")
		}
	}
}

func write(t *testing.T, root, relative, value string) {
	t.Helper()
	path := filepath.Join(root, filepath.FromSlash(relative))
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(value), 0o644); err != nil {
		t.Fatal(err)
	}
}

func runGit(t *testing.T, root string, args ...string) {
	t.Helper()
	command := exec.Command("git", args...)
	command.Dir = root
	if output, err := command.CombinedOutput(); err != nil {
		t.Fatalf("git %v: %v: %s", args, err, output)
	}
}
