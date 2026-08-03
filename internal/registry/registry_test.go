package registry

import "testing"

func TestCompareCountsCompressedBlobReuseAndOrder(t *testing.T) {
	previous := []Layer{{Digest: "a", Size: 10}, {Digest: "b", Size: 20}}
	current := []Layer{{Digest: "b", Size: 20}, {Digest: "c", Size: 30}}
	result := Compare(current, previous, true)
	if result.MatchingBlobs != 1 || result.UnmatchedBlobs != 1 || result.UnmatchedCompressedBytes != 30 {
		t.Fatalf("comparison = %#v", result)
	}
	if result.ChangedPositions != 2 || result.CommonPrefix != 0 {
		t.Fatalf("ordered comparison = %#v", result)
	}
}
