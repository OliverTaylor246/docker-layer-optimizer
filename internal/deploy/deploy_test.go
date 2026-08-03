package deploy

import (
	"testing"
	"time"
)

func TestWendyCommandForcesLayerDiffAndExactTimingsOverrideMarkers(t *testing.T) {
	command := PrepareCommand([]string{"wendy", "run", "--device", "Woof"}, "wendy")
	want := []string{"wendy", "run", "--chunking", "force", "--device", "Woof"}
	if !same(command, want) {
		t.Fatalf("command=%#v want=%#v", command, want)
	}
	unchanged := PrepareCommand([]string{"wendy", "run", "--chunking=off"}, "wendy")
	if !same(unchanged, []string{"wendy", "run", "--chunking=off"}) {
		t.Fatalf("explicit mode changed: %#v", unchanged)
	}

	tracker, err := NewTracker("wendy", nil)
	if err != nil {
		t.Fatal(err)
	}
	start := time.Unix(100, 0)
	tracker.Start(start)
	tracker.Feed("Building image (OCI layout) for linux/arm64...", start)
	tracker.Feed("[timing] build (oci export) 3.514s", start.Add(time.Second))
	tracker.Feed("Diffing 10 layer(s) against device...", start.Add(2*time.Second))
	tracker.Feed("[timing] chunk+query+write 147ms", start.Add(3*time.Second))
	tracker.Feed("Application app running in detached mode.", start.Add(4*time.Second))
	tracker.Feed("Waiting for host:8110 to be ready...", start.Add(5*time.Second))
	tracker.Feed("[timing] runcontainer (assemble+create+start[+readiness]) 1.600s", start.Add(5*time.Second))
	tracker.Feed("Ready.", start.Add(6*time.Second))
	result := tracker.Finish(start.Add(6 * time.Second))
	if result.PhaseSource != "wendy-timing+output-markers" || result.Phases["build"].DurationSeconds != 3.514 || result.Phases["transfer"].DurationSeconds != 0.147 {
		t.Fatalf("timings=%#v", result)
	}
	if result.Phases["replacement"].DurationSeconds != 0.6 || result.Phases["readiness"].DurationSeconds != 1 {
		t.Fatalf("runtime phases=%#v", result.Phases)
	}
}

func same(left, right []string) bool {
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
