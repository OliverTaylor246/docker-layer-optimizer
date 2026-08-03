package dockerfile

import (
	"strings"
	"testing"
)

func TestParseUsesDockerfileSemanticsForStagesFlagsAndContinuations(t *testing.T) {
	input := `# syntax=docker/dockerfile:1.7
FROM golang:1.24 AS build
WORKDIR /src
COPY --link ["go.mod", "go.sum", "./"]
RUN --mount=type=cache,target=/go/pkg/mod \
    go mod download
COPY . .
FROM scratch
COPY --from=build /src/app /app
`

	instructions, err := Parse(strings.NewReader(input))
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}
	if len(instructions) != 7 {
		t.Fatalf("got %d instructions, want 7: %#v", len(instructions), instructions)
	}

	copyManifest := instructions[2]
	if copyManifest.Command != "COPY" || copyManifest.Stage != 0 || copyManifest.StartLine != 4 || copyManifest.EndLine != 4 {
		t.Fatalf("manifest COPY metadata = %#v", copyManifest)
	}
	if got := copyManifest.ContextSources(); !equalStrings(got, []string{"go.mod", "go.sum"}) {
		t.Fatalf("manifest COPY sources = %#v", got)
	}

	run := instructions[3]
	if run.StartLine != 5 || run.EndLine != 6 || !strings.Contains(run.Original, "go mod download") {
		t.Fatalf("continued RUN metadata = %#v", run)
	}

	broadCopy := instructions[4]
	if got := broadCopy.ContextSources(); !equalStrings(got, []string{"."}) {
		t.Fatalf("broad COPY sources = %#v", got)
	}

	stageCopy := instructions[6]
	if stageCopy.Stage != 1 || stageCopy.ContextSources() != nil {
		t.Fatalf("stage COPY must not be treated as build-context input: %#v", stageCopy)
	}
}

func TestParsePreservesHeredocAsOneInstruction(t *testing.T) {
	input := "FROM alpine\nRUN <<'EOF'\necho hello\nEOF\n"
	instructions, err := Parse(strings.NewReader(input))
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}
	if len(instructions) != 2 {
		t.Fatalf("got %d instructions, want 2", len(instructions))
	}
	if instructions[1].StartLine != 2 || instructions[1].EndLine != 4 {
		t.Fatalf("heredoc lines = %d-%d, want 2-4", instructions[1].StartLine, instructions[1].EndLine)
	}
}

func equalStrings(left, right []string) bool {
	if len(left) != len(right) {
		return false
	}
	for index := range left {
		if left[index] != right[index] {
			return false
		}
	}
	return true
}
