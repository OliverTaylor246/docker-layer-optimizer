package cli

import (
	"bytes"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
)

func TestAnalyzeRecordAndHistoryPreserveAgentFacingCommands(t *testing.T) {
	root := t.TempDir()
	cache := t.TempDir()
	t.Setenv("DLO_CACHE_DIR", cache)
	git(t, root, "init", "-q")
	git(t, root, "config", "user.name", "Test")
	git(t, root, "config", "user.email", "test@example.com")
	writeCLI(t, root, "Dockerfile", "FROM alpine\nCOPY . /app\n")
	writeCLI(t, root, "app.txt", "one\n")
	git(t, root, "add", ".")
	git(t, root, "commit", "-qm", "initial")

	var stdout, stderr bytes.Buffer
	if code := Execute([]string{"analyze", "--root", root, "--json"}, &stdout, &stderr); code != 0 {
		t.Fatalf("analyze code=%d stderr=%s", code, stderr.String())
	}
	var report map[string]any
	if err := json.Unmarshal(stdout.Bytes(), &report); err != nil {
		t.Fatalf("analyze JSON: %v: %s", err, stdout.String())
	}
	if report["schema_version"] != float64(3) {
		t.Fatalf("schema_version = %#v", report["schema_version"])
	}

	writeCLI(t, root, "app.txt", "two\n")
	stdout.Reset()
	stderr.Reset()
	if code := Execute([]string{"record", "--root", root, "--kind", "task", "--from-git", "--tag", "Source Code", "--json"}, &stdout, &stderr); code != 0 {
		t.Fatalf("record code=%d stderr=%s", code, stderr.String())
	}
	var recorded map[string]any
	if err := json.Unmarshal(stdout.Bytes(), &recorded); err != nil {
		t.Fatal(err)
	}
	event := recorded["event"].(map[string]any)
	paths := event["changed_paths"].([]any)
	if len(paths) != 1 || paths[0] != "app.txt" {
		t.Fatalf("changed_paths = %#v", paths)
	}

	stdout.Reset()
	stderr.Reset()
	if code := Execute([]string{"history", "--root", root, "--json"}, &stdout, &stderr); code != 0 {
		t.Fatalf("history code=%d stderr=%s", code, stderr.String())
	}
	var events []map[string]any
	if err := json.Unmarshal(stdout.Bytes(), &events); err != nil || len(events) != 1 {
		t.Fatalf("history = %#v err=%v output=%s", events, err, stdout.String())
	}
}

func TestOptimizePlanUsesAgentFacingCommand(t *testing.T) {
	root := t.TempDir()
	git(t, root, "init", "-q")
	git(t, root, "config", "user.name", "Test")
	git(t, root, "config", "user.email", "test@example.com")
	writeCLI(t, root, "Dockerfile", "FROM python:3.12-slim\nWORKDIR /app\nCOPY . .\nRUN pip install -r requirements.txt\nCMD [\"python\", \"app.py\"]\n")
	writeCLI(t, root, "requirements.txt", "flask==3.0.0\n")
	writeCLI(t, root, "app.py", "print('ready')\n")
	git(t, root, "add", ".")
	git(t, root, "commit", "-qm", "initial")

	var stdout, stderr bytes.Buffer
	code := Execute([]string{"optimize", "--root", root, "--plan", "--json"}, &stdout, &stderr)
	if code != 0 {
		t.Fatalf("optimize code=%d stderr=%s", code, stderr.String())
	}
	var result map[string]any
	if err := json.Unmarshal(stdout.Bytes(), &result); err != nil {
		t.Fatalf("optimize JSON: %v: %s", err, stdout.String())
	}
	candidate, ok := result["candidate"].(map[string]any)
	if !ok || candidate["kind"] != "manifest-first" {
		t.Fatalf("candidate = %#v", result["candidate"])
	}

	stdout.Reset()
	stderr.Reset()
	candidateID, _ := candidate["candidate_id"].(string)
	code = Execute([]string{"optimize", "--root", root, "--apply-approved", candidateID, "--json"}, &stdout, &stderr)
	if code != 0 {
		t.Fatalf("apply-approved code=%d stderr=%s stdout=%s", code, stderr.String(), stdout.String())
	}
	optimized, err := os.ReadFile(filepath.Join(root, "Dockerfile"))
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Contains(optimized, []byte(`COPY ["requirements.txt", "./"]`)) {
		t.Fatalf("approved candidate was not applied:\n%s", optimized)
	}
}

func TestJSONDocuments(t *testing.T) {
	root := filepath.Join("..", "..")
	paths := []string{
		".codex-plugin/plugin.json",
		".claude-plugin/plugin.json",
		".claude-plugin/marketplace.json",
		".agents/plugins/marketplace.json",
		"docs/observation-schema-v3.json",
		"docs/optimization-schema-v1.json",
	}
	for _, relative := range paths {
		data, err := os.ReadFile(filepath.Join(root, relative))
		if err != nil {
			t.Fatalf("read %s: %v", relative, err)
		}
		var value any
		if err := json.Unmarshal(data, &value); err != nil {
			t.Errorf("invalid JSON %s: %v", relative, err)
		}
	}
}

func writeCLI(t *testing.T, root, relative, value string) {
	t.Helper()
	path := filepath.Join(root, relative)
	if err := os.WriteFile(path, []byte(value), 0o644); err != nil {
		t.Fatal(err)
	}
}

func git(t *testing.T, root string, args ...string) {
	t.Helper()
	command := exec.Command("git", args...)
	command.Dir = root
	if output, err := command.CombinedOutput(); err != nil {
		t.Fatalf("git %v: %v: %s", args, err, output)
	}
}
