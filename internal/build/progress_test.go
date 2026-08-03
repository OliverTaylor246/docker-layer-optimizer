package build

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestRawJSONProgressCountsCacheAndRedactsInstructionText(t *testing.T) {
	parser := NewRawJSONParser()
	secret := "DO_NOT_PERSIST_123"
	events := []map[string]any{
		{"vertexes": []map[string]any{{"digest": "context", "name": "[internal] load build context"}}},
		{"statuses": []map[string]any{{"id": "transferring context:", "vertex": "context", "current": 4096}}},
		{"vertexes": []map[string]any{
			{"digest": "from", "name": "[1/3] FROM alpine", "started": "2026-01-01T00:00:00Z", "completed": "2026-01-01T00:00:00.01Z"},
			{"digest": "copy", "name": "[2/3] COPY app /app", "started": "2026-01-01T00:00:00Z", "completed": "2026-01-01T00:00:00Z", "cached": true},
			{"digest": "run", "name": "[3/3] RUN echo " + secret, "started": "2026-01-01T00:00:00Z", "completed": "2026-01-01T00:00:00.2Z"},
		}},
	}
	for _, event := range events {
		value, _ := json.Marshal(event)
		parser.Feed(string(value))
	}
	summary := parser.Summary()
	if summary.Cached != 1 || summary.Rebuilt != 1 || summary.Resolved != 1 || parser.ContextBytes() != 4096 {
		t.Fatalf("summary=%#v context=%d", summary, parser.ContextBytes())
	}
	serialized, _ := json.Marshal(summary)
	if strings.Contains(string(serialized), secret) || !strings.Contains(string(serialized), "instruction_sha256") {
		t.Fatalf("unsafe summary: %s", serialized)
	}
}

func TestBuildCommandPreservesProductionBuildxOptions(t *testing.T) {
	options := Options{
		Root: "/project", Dockerfile: "/project/Dockerfile.prod", Tag: "registry/app:test",
		Platform: "linux/arm64", Builder: "remote", Push: true, NoCache: true,
		BuildArgs: []string{"MODE=prod"}, Secrets: []string{"id=token,src=/tmp/token"},
		SSH: []string{"default"}, CacheFrom: []string{"type=registry,ref=cache"},
		BuildContexts: []string{"assets=../assets"},
	}
	command := Command(options, "/tmp/metadata.json", "rawjson")
	joined := strings.Join(command, " ")
	for _, expected := range []string{
		"docker buildx build", "--progress=rawjson", "--file /project/Dockerfile.prod",
		"--push", "--platform linux/arm64", "--builder remote", "--no-cache",
		"--build-arg MODE=prod", "--secret id=token,src=/tmp/token", "--ssh default",
		"--cache-from type=registry,ref=cache", "--build-context assets=../assets",
	} {
		if !strings.Contains(joined, expected) {
			t.Fatalf("command missing %q: %s", expected, joined)
		}
	}
}

func TestSnapshotContextUsesDockerignoreNegation(t *testing.T) {
	root := t.TempDir()
	writeBuildTest(t, filepath.Join(root, ".dockerignore"), "dist/*\n!dist/keep.txt\n")
	writeBuildTest(t, filepath.Join(root, "app.go"), "package main\n")
	writeBuildTest(t, filepath.Join(root, "dist/drop.txt"), "drop\n")
	writeBuildTest(t, filepath.Join(root, "dist/keep.txt"), "keep\n")
	snapshot, err := SnapshotContext(root, filepath.Join(root, "Dockerfile"))
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := snapshot["dist/drop.txt"]; ok {
		t.Fatal("ignored file entered snapshot")
	}
	if _, ok := snapshot["dist/keep.txt"]; !ok {
		t.Fatal("negated file missing from snapshot")
	}
}

func writeBuildTest(t *testing.T, path, value string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(value), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestPlainProgressAndLayerComparisonRemainCompatible(t *testing.T) {
	parser := NewPlainParser()
	for _, line := range []string{
		"#1 [1/3] FROM alpine", "#1 CACHED", "#2 [2/3] COPY . /app", "#2 DONE 0.2s",
		"#3 [3/3] RUN false", "#3 ERROR: process failed", "#4 transferring context: 2.45kB done",
	} {
		parser.Feed(line)
	}
	summary := parser.Summary()
	if summary.Cached != 1 || summary.Rebuilt != 1 || summary.Failed != 1 || parser.ContextBytes() != 2450 {
		t.Fatalf("summary=%#v context=%d", summary, parser.ContextBytes())
	}
	comparison := CompareLayers([]string{"b", "a"}, []string{"a", "b"}, true)
	if comparison.MatchingDiffIDs != 2 || comparison.UnmatchedDiffIDs != 0 || comparison.ChangedPositions != 2 || comparison.CommonPrefix != 0 {
		t.Fatalf("comparison=%#v", comparison)
	}
}
