package integration

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/wendylabsinc/docker-layer-optimizer/internal/cli"
)

func TestDockerBuildAndVerifiedOptimizationLifecycle(t *testing.T) {
	if os.Getenv("DLO_DOCKER_INTEGRATION") != "1" {
		t.Skip("set DLO_DOCKER_INTEGRATION=1 to run real Docker integration")
	}
	if err := exec.Command("docker", "buildx", "version").Run(); err != nil {
		t.Skipf("Docker Buildx unavailable: %v", err)
	}
	root := t.TempDir()
	cache := t.TempDir()
	t.Setenv("DLO_CACHE_DIR", cache)
	write(t, root, "Dockerfile", "FROM alpine:3.20\nWORKDIR /app\nCOPY . .\nRUN sleep 1 && apk add --no-cache ca-certificates\nCMD [\"cat\", \"app.go\"]\n")
	write(t, root, "requirements.txt", "runtime-dependency\n")
	write(t, root, "app.go", "package main\n")
	git(t, root, "init", "-q")
	git(t, root, "config", "user.name", "DLO Integration")
	git(t, root, "config", "user.email", "integration@wendy.dev")
	git(t, root, "add", ".")
	git(t, root, "commit", "-qm", "fixture")
	tag := fmt.Sprintf("dlo-integration:%d", time.Now().UnixNano())
	t.Cleanup(func() { _ = exec.Command("docker", "image", "rm", "-f", tag).Run() })

	result := run(t, "build", "--root", root, "--tag", tag, "--json", "--quiet")
	if result["kind"] != "build" || result["status"] != "success" {
		t.Fatalf("build result = %#v", result)
	}

	write(t, root, ".dlo.yml", "version: 1\nverification:\n  commands:\n    - test -f app.go\nbenchmark:\n  source_path: app.go\n  trials: 3\n  budget_seconds: 180\n  min_relative_improvement: 0.10\n  min_absolute_seconds: 0.20\n  max_relative_regression: 0.50\n  max_absolute_regression_seconds: 1.0\n  payback_deploys: 50\n")
	outcome := run(t, "optimize", "--root", root, "--force", "--json")
	if outcome["status"] != "verified-applied" || outcome["applied"] != true {
		t.Fatalf("optimization outcome = %#v", outcome)
	}
	data, err := os.ReadFile(filepath.Join(root, "Dockerfile"))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(data), `COPY ["requirements.txt", "./"]`) {
		t.Fatalf("optimized Dockerfile missing manifest-first copy:\n%s", data)
	}
}

func run(t *testing.T, args ...string) map[string]any {
	t.Helper()
	var stdout, stderr bytes.Buffer
	if code := cli.Execute(args, &stdout, &stderr); code != 0 {
		t.Fatalf("dlo %v code=%d stderr=%s stdout=%s", args, code, stderr.String(), stdout.String())
	}
	var result map[string]any
	if err := json.Unmarshal(stdout.Bytes(), &result); err != nil {
		t.Fatalf("decode %v: %v: %s", args, err, stdout.String())
	}
	return result
}

func write(t *testing.T, root, name, value string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(root, name), []byte(value), 0o644); err != nil {
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
