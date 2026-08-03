// Package state owns DLO's privacy-safe, project-scoped local observation store.
package state

import (
	"bufio"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"strings"

	"github.com/gofrs/flock"
)

const eventsFile = "events.jsonl"

var unsafeSlug = regexp.MustCompile(`[^a-z0-9_.-]+`)

// Store persists schema-compatible JSON events without retaining source text or
// build output. A process-safe file lock keeps concurrent agent observations
// from interleaving.
type Store struct {
	Root string
	Dir  string
}

func Open(root string) (*Store, error) {
	absolute, err := filepath.Abs(root)
	if err != nil {
		return nil, err
	}
	base := os.Getenv("DLO_CACHE_DIR")
	if base == "" {
		base, err = os.UserCacheDir()
		if err != nil {
			return nil, err
		}
		base = filepath.Join(base, "docker-layer-optimizer")
	}
	slug := strings.Trim(unsafeSlug.ReplaceAllString(strings.ToLower(filepath.Base(absolute)), "-"), "-._")
	if slug == "" {
		slug = "project"
	}
	digest := sha256.Sum256([]byte(absolute))
	dir := filepath.Join(base, fmt.Sprintf("%s-%s", slug, hex.EncodeToString(digest[:8])))
	return &Store{Root: absolute, Dir: dir}, nil
}

func (store *Store) Path() string { return filepath.Join(store.Dir, eventsFile) }

func (store *Store) Append(event map[string]any) (string, error) {
	if err := os.MkdirAll(store.Dir, 0o700); err != nil {
		return "", err
	}
	_ = os.Chmod(store.Dir, 0o700)
	lock := flock.New(filepath.Join(store.Dir, "state.lock"))
	if err := lock.Lock(); err != nil {
		return "", err
	}
	defer func() { _ = lock.Unlock() }()

	path := store.Path()
	handle, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		return "", err
	}
	encoder := json.NewEncoder(handle)
	encoder.SetEscapeHTML(false)
	encodeErr := encoder.Encode(event)
	closeErr := handle.Close()
	_ = os.Chmod(path, 0o600)
	if encodeErr != nil {
		return "", encodeErr
	}
	return path, closeErr
}

func (store *Store) Load(limit int) ([]map[string]any, error) {
	if limit <= 0 {
		return []map[string]any{}, nil
	}
	handle, err := os.Open(store.Path())
	if os.IsNotExist(err) {
		return []map[string]any{}, nil
	}
	if err != nil {
		return nil, err
	}
	defer handle.Close()
	events := make([]map[string]any, 0, limit)
	scanner := bufio.NewScanner(handle)
	buffer := make([]byte, 64*1024)
	scanner.Buffer(buffer, 16*1024*1024)
	for scanner.Scan() {
		var event map[string]any
		if json.Unmarshal(scanner.Bytes(), &event) == nil {
			events = append(events, event)
			if len(events) > limit {
				events = events[1:]
			}
		}
	}
	return events, scanner.Err()
}

// PlatformCacheRoot is exposed for diagnostics and parity tests.
func PlatformCacheRoot() string {
	if override := os.Getenv("DLO_CACHE_DIR"); override != "" {
		return override
	}
	value, _ := os.UserCacheDir()
	if runtime.GOOS == "windows" && value == "" {
		return filepath.Join(os.Getenv("LOCALAPPDATA"), "docker-layer-optimizer")
	}
	return filepath.Join(value, "docker-layer-optimizer")
}
