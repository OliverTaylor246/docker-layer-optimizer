// Package dockerfile exposes DLO's Dockerfile model while delegating syntax
// parsing to BuildKit, the same implementation Docker uses for Dockerfiles.
package dockerfile

import (
	"fmt"
	"io"
	"strings"

	buildkitinstructions "github.com/moby/buildkit/frontend/dockerfile/instructions"
	buildkitparser "github.com/moby/buildkit/frontend/dockerfile/parser"
)

// Instruction is the stable model consumed by DLO's analyzer. Syntax details
// remain inside this package so callers do not need to understand BuildKit's
// linked AST representation.
type Instruction struct {
	Command   string
	Args      string
	Original  string
	Stage     int
	StartLine int
	EndLine   int

	contextSources []string
	contextDest    string
	contextInput   bool
}

// ContextDestination returns the destination of a local COPY or ADD.
func (instruction Instruction) ContextDestination() string {
	if !instruction.contextInput {
		return ""
	}
	return instruction.contextDest
}

// ContextSources returns the paths read from the local build context by a COPY
// or ADD instruction. It returns nil for other instructions and for COPY
// --from, whose source belongs to an image or prior build stage.
func (instruction Instruction) ContextSources() []string {
	if !instruction.contextInput {
		return nil
	}
	return append([]string(nil), instruction.contextSources...)
}

// Parse parses a Dockerfile with BuildKit's canonical parser and typed
// instruction decoder. That gives DLO Docker-compatible handling for escape
// directives, continuations, JSON forms, flags, heredocs, and future syntax.
func Parse(reader io.Reader) ([]Instruction, error) {
	result, err := buildkitparser.Parse(reader)
	if err != nil {
		return nil, fmt.Errorf("parse Dockerfile with BuildKit: %w", err)
	}

	stage := -1
	instructions := make([]Instruction, 0, len(result.AST.Children))
	for _, node := range result.AST.Children {
		command := strings.ToUpper(node.Value)
		if command == "FROM" {
			stage++
		}
		currentStage := stage
		if currentStage < 0 {
			currentStage = 0
		}
		original := strings.TrimSpace(node.Original)
		instruction := Instruction{
			Command:   command,
			Args:      instructionArgs(original),
			Original:  original,
			Stage:     currentStage,
			StartLine: node.StartLine,
			EndLine:   node.EndLine,
		}

		typed, parseErr := buildkitinstructions.ParseInstruction(node)
		if parseErr != nil {
			return nil, fmt.Errorf("decode Dockerfile instruction at line %d: %w", node.StartLine, parseErr)
		}
		switch value := typed.(type) {
		case *buildkitinstructions.CopyCommand:
			if value.From == "" {
				instruction.contextInput = true
				instruction.contextSources = append([]string(nil), value.SourcePaths...)
				instruction.contextDest = value.DestPath
			}
		case *buildkitinstructions.AddCommand:
			instruction.contextInput = true
			instruction.contextSources = append([]string(nil), value.SourcePaths...)
			instruction.contextDest = value.DestPath
		}
		instructions = append(instructions, instruction)
	}
	return instructions, nil
}

func instructionArgs(original string) string {
	index := strings.IndexAny(original, " \t\r\n")
	if index < 0 {
		return ""
	}
	return strings.TrimSpace(original[index:])
}
