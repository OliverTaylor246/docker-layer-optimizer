// Package registry inspects pushed OCI manifests and compares compressed blobs.
package registry

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os/exec"
	"strings"
)

type Layer struct {
	Digest    string `json:"digest"`
	Size      int64  `json:"size"`
	MediaType string `json:"media_type,omitempty"`
}

type Comparison struct {
	TotalLayers              int   `json:"total_layers"`
	MatchingBlobs            int   `json:"matching_blobs"`
	UnmatchedBlobs           int   `json:"unmatched_blobs"`
	RemovedBlobs             int   `json:"removed_blobs"`
	ChangedPositions         int   `json:"changed_positions"`
	CommonPrefix             int   `json:"common_prefix"`
	TotalCompressedBytes     int64 `json:"total_compressed_bytes"`
	MatchingCompressedBytes  int64 `json:"matching_compressed_bytes"`
	UnmatchedCompressedBytes int64 `json:"unmatched_compressed_bytes"`
	HasBaseline              bool  `json:"has_baseline"`
}

type Inspection struct {
	Reference      string         `json:"reference"`
	IndexDigest    *string        `json:"index_digest"`
	ManifestDigest string         `json:"manifest_digest"`
	MediaType      string         `json:"media_type,omitempty"`
	Platform       string         `json:"platform,omitempty"`
	Config         map[string]any `json:"config,omitempty"`
	Layers         []Layer        `json:"layers"`
	Comparison
}

type manifest struct {
	MediaType string         `json:"mediaType"`
	Config    map[string]any `json:"config"`
	Layers    []struct {
		Digest    string `json:"digest"`
		Size      int64  `json:"size"`
		MediaType string `json:"mediaType"`
	} `json:"layers"`
	Manifests []descriptor `json:"manifests"`
}

type descriptor struct {
	Digest      string            `json:"digest"`
	Platform    map[string]string `json:"platform"`
	Annotations map[string]string `json:"annotations"`
}

func Compare(current, previous []Layer, hasBaseline bool) Comparison {
	available := map[string]int{}
	for _, layer := range previous {
		available[layer.Digest]++
	}
	matching, matchingBytes := 0, int64(0)
	for _, layer := range current {
		if available[layer.Digest] > 0 {
			available[layer.Digest]--
			matching++
			matchingBytes += layer.Size
		}
	}
	unchanged, prefix, totalBytes := 0, 0, int64(0)
	for index, layer := range current {
		totalBytes += layer.Size
		if index < len(previous) && layer.Digest == previous[index].Digest {
			unchanged++
		}
	}
	for prefix < len(current) && prefix < len(previous) && current[prefix].Digest == previous[prefix].Digest {
		prefix++
	}
	maximum := len(current)
	if len(previous) > maximum {
		maximum = len(previous)
	}
	return Comparison{len(current), matching, len(current) - matching, len(previous) - matching, maximum - unchanged, prefix, totalBytes, matchingBytes, totalBytes - matchingBytes, hasBaseline}
}

func Inspect(reference, platform string, previous []Layer, hasBaseline bool) (Inspection, error) {
	value, raw, err := inspectRaw(reference)
	if err != nil {
		return Inspection{}, err
	}
	indexDigest := digest(raw)
	manifestDigest := indexDigest
	selectedPlatform := platform
	if len(value.Manifests) > 0 {
		descriptor, selectErr := selectDescriptor(value.Manifests, platform)
		if selectErr != nil {
			return Inspection{}, selectErr
		}
		value, raw, err = inspectRaw(digestReference(reference, descriptor.Digest))
		if err != nil {
			return Inspection{}, err
		}
		manifestDigest = descriptor.Digest
		parts := []string{descriptor.Platform["os"], descriptor.Platform["architecture"], descriptor.Platform["variant"]}
		var kept []string
		for _, part := range parts {
			if part != "" {
				kept = append(kept, part)
			}
		}
		if len(kept) > 0 {
			selectedPlatform = strings.Join(kept, "/")
		}
		_ = raw
	}
	layers := make([]Layer, 0, len(value.Layers))
	for _, item := range value.Layers {
		if item.Digest != "" {
			layers = append(layers, Layer{item.Digest, item.Size, item.MediaType})
		}
	}
	if len(layers) == 0 {
		return Inspection{}, fmt.Errorf("no filesystem layers found in pushed image manifest for %s", reference)
	}
	var index *string
	if indexDigest != manifestDigest {
		index = &indexDigest
	}
	return Inspection{Reference: reference, IndexDigest: index, ManifestDigest: manifestDigest, MediaType: value.MediaType, Platform: selectedPlatform, Config: value.Config, Layers: layers, Comparison: Compare(layers, previous, hasBaseline)}, nil
}

func inspectRaw(reference string) (manifest, []byte, error) {
	command := exec.Command("docker", "buildx", "imagetools", "inspect", "--raw", reference)
	raw, err := command.Output()
	if err != nil {
		if exit, ok := err.(*exec.ExitError); ok {
			return manifest{}, nil, fmt.Errorf("%s", strings.TrimSpace(string(exit.Stderr)))
		}
		return manifest{}, nil, err
	}
	raw = []byte(strings.TrimRight(string(raw), "\r\n"))
	var value manifest
	if err := json.Unmarshal(raw, &value); err != nil {
		return manifest{}, nil, fmt.Errorf("registry returned invalid manifest JSON for %s", reference)
	}
	return value, raw, nil
}

func selectDescriptor(values []descriptor, platform string) (descriptor, error) {
	parts := strings.Split(platform, "/")
	candidates := make([]descriptor, 0)
	for _, value := range values {
		if value.Annotations["vnd.docker.reference.type"] != "" || value.Platform["os"] == "unknown" {
			continue
		}
		if platform != "" {
			if len(parts) < 2 {
				return descriptor{}, fmt.Errorf("platform must be os/architecture[/variant], got %q", platform)
			}
			if value.Platform["os"] != parts[0] || value.Platform["architecture"] != parts[1] {
				continue
			}
			if len(parts) > 2 && value.Platform["variant"] != parts[2] {
				continue
			}
		}
		candidates = append(candidates, value)
	}
	if len(candidates) != 1 {
		return descriptor{}, fmt.Errorf("expected one runnable image manifest for platform %s, found %d", platform, len(candidates))
	}
	return candidates[0], nil
}

func digest(raw []byte) string {
	value := sha256.Sum256(raw)
	return "sha256:" + hex.EncodeToString(value[:])
}
func digestReference(reference, digest string) string {
	base := strings.Split(reference, "@")[0]
	slash := strings.LastIndex(base, "/")
	colon := strings.LastIndex(base, ":")
	if colon > slash {
		base = base[:colon]
	}
	return base + "@" + digest
}
