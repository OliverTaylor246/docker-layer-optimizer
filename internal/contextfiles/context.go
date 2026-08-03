// Package contextfiles computes the effective tracked Docker build context.
package contextfiles

import (
	"bufio"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/moby/patternmatcher"
	"github.com/moby/patternmatcher/ignorefile"
)

func IgnorePath(root, dockerfile string) string {
	specific := dockerfile + ".dockerignore"
	if info, err := os.Stat(specific); err == nil && info.Mode().IsRegular() {
		return specific
	}
	return filepath.Join(root, ".dockerignore")
}

func Patterns(root, dockerfile string) ([]string, error) {
	path := IgnorePath(root, dockerfile)
	handle, err := os.Open(path)
	if os.IsNotExist(err) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	defer handle.Close()
	return ignorefile.ReadAll(handle)
}

// Filter removes paths Docker excludes, using Moby's pattern matcher rather
// than DLO-specific glob semantics.
func Filter(root, dockerfile string, files []string) ([]string, error) {
	patterns, err := Patterns(root, dockerfile)
	if err != nil {
		return nil, err
	}
	matcher, err := patternmatcher.New(patterns)
	if err != nil {
		return nil, err
	}
	excluded := map[string]bool{}
	for _, path := range []string{dockerfile, IgnorePath(root, dockerfile)} {
		if relative, relErr := filepath.Rel(root, path); relErr == nil && relative != "." && !strings.HasPrefix(relative, "..") {
			excluded[filepath.ToSlash(relative)] = true
		}
	}
	result := make([]string, 0, len(files))
	for _, file := range files {
		normalized := filepath.ToSlash(file)
		ignored, matchErr := matcher.MatchesOrParentMatches(normalized)
		if matchErr != nil {
			return nil, matchErr
		}
		if !ignored && !excluded[normalized] {
			result = append(result, normalized)
		}
	}
	sort.Strings(result)
	return result, nil
}

// ReadPatterns is retained as a small inspection helper for agents.
func ReadPatterns(path string) ([]string, error) {
	handle, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer handle.Close()
	var values []string
	scanner := bufio.NewScanner(handle)
	for scanner.Scan() {
		values = append(values, scanner.Text())
	}
	return values, scanner.Err()
}
