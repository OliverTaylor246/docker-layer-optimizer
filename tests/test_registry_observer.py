import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "skills" / "optimize-docker-layers" / "scripts" / "registry_observer.py"
SPEC = importlib.util.spec_from_file_location("registry_observer", SCRIPT)
registry = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = registry
SPEC.loader.exec_module(registry)


class RegistryObserverTests(unittest.TestCase):
    def test_compare_registry_layers_counts_blobs_bytes_and_positions(self):
        previous = [{"digest": "a", "size": 10}, {"digest": "b", "size": 20}]
        current = [{"digest": "a", "size": 10}, {"digest": "c", "size": 30}]
        result = registry.compare_registry_layers(current, previous)
        self.assertEqual(result["matching_blobs"], 1)
        self.assertEqual(result["unmatched_blobs"], 1)
        self.assertEqual(result["unmatched_compressed_bytes"], 30)
        self.assertEqual(result["changed_positions"], 1)

    def test_select_platform_ignores_attestations(self):
        index = {"manifests": [
            {"digest": "att", "platform": {"os": "unknown", "architecture": "unknown"}},
            {"digest": "arm", "platform": {"os": "linux", "architecture": "arm64"}},
            {"digest": "amd", "platform": {"os": "linux", "architecture": "amd64"}},
        ]}
        selected = registry.select_platform_descriptor(index, "linux/arm64")
        self.assertEqual(selected["digest"], "arm")

    def test_inspect_registry_image_follows_index_manifest(self):
        index = {
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [{"digest": "sha256:arm", "platform": {"os": "linux", "architecture": "arm64"}}],
        }
        manifest = {
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {"digest": "sha256:config", "size": 5},
            "layers": [{"digest": "sha256:one", "size": 123, "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip"}],
        }
        with mock.patch.object(registry, "_run_imagetools", side_effect=[(index, json.dumps(index).encode()), (manifest, json.dumps(manifest).encode())]) as inspect:
            result = registry.inspect_registry_image("localhost:5000/team/app:tag", "linux/arm64")
        self.assertEqual(inspect.call_args_list[1].args[0], "localhost:5000/team/app@sha256:arm")
        self.assertEqual(result["total_compressed_bytes"], 123)
        self.assertEqual(result["unmatched_blobs"], 1)


if __name__ == "__main__":
    unittest.main()
