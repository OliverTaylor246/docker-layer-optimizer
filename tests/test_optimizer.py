import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "skills" / "optimize-docker-layers" / "scripts" / "docker_layer_optimizer.py"
SCRIPTS = SCRIPT.parent
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("docker_layer_optimizer", SCRIPT)
optimizer = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = optimizer
SPEC.loader.exec_module(optimizer)
import build_observer
import deployment_observer


def run(root: Path, *args: str) -> None:
    subprocess.run(args, cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


class OptimizerTests(unittest.TestCase):
    def setUp(self):
        self.temporary_paths = []
        self.cache = Path(tempfile.mkdtemp()).resolve()
        self.temporary_paths.append(self.cache)
        self.previous_cache = os.environ.get("DLO_CACHE_DIR")
        os.environ["DLO_CACHE_DIR"] = str(self.cache)

    def tearDown(self):
        if self.previous_cache is None:
            os.environ.pop("DLO_CACHE_DIR", None)
        else:
            os.environ["DLO_CACHE_DIR"] = self.previous_cache
        for path in self.temporary_paths:
            shutil.rmtree(path, ignore_errors=True)

    def make_repo(self) -> Path:
        root = Path(tempfile.mkdtemp())
        self.temporary_paths.append(root)
        run(root, "git", "init", "-q")
        run(root, "git", "config", "user.name", "Test")
        run(root, "git", "config", "user.email", "test@example.com")
        (root / "requirements.txt").write_text("flask==3.0.0\n", encoding="utf-8")
        (root / "app.py").write_text("print('one')\n", encoding="utf-8")
        (root / "Dockerfile").write_text(
            "FROM python:3.12-slim\nCOPY . /app\nRUN pip install -r /app/requirements.txt\nCMD [\"python\", \"/app/app.py\"]\n",
            encoding="utf-8",
        )
        run(root, "git", "add", ".")
        run(root, "git", "commit", "-qm", "initial")
        for index in range(3):
            (root / "app.py").write_text(f"print({index})\n", encoding="utf-8")
            run(root, "git", "add", "app.py")
            run(root, "git", "commit", "-qm", f"app {index}")
        return root

    def test_history_finds_broad_copy_before_dependency_install(self):
        root = self.make_repo()
        report = optimizer.analyze(root, root / "Dockerfile", 100)
        self.assertEqual(report["evidence"]["commits"], 4)
        self.assertGreater(report["layers"][0]["change_likelihood"], 0.5)
        self.assertIn("split-dependency-inputs", {item["kind"] for item in report["recommendations"]})

    def test_record_stays_in_user_cache_outside_repository(self):
        root = self.make_repo()
        (root / "app.py").write_text("print('changed')\n", encoding="utf-8")
        args = optimizer.parser().parse_args([
            "record", "--root", str(root), "--kind", "task", "--from-git", "--tag", "Source Code",
        ])
        result = optimizer.record(args)
        state_file = Path(result["state_file"])
        self.assertTrue(state_file.is_file())
        self.assertTrue(state_file.is_relative_to(self.cache))
        self.assertFalse(state_file.is_relative_to(root))
        event = json.loads(state_file.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(event["changed_paths"], ["app.py"])
        self.assertEqual(event["tags"], ["source-code"])
        history = build_observer.render_history([event])
        self.assertIn("1 changed paths", history)
        self.assertIn("tags source-code", history)

    def test_copy_from_stage_is_not_context_input(self):
        instruction = optimizer.Instruction("COPY", "--from=builder /out/app /app", 7, "COPY --from=builder /out/app /app", 1)
        self.assertIsNone(optimizer.parse_copy_sources(instruction))

    def test_json_copy_with_flags(self):
        instruction = optimizer.Instruction(
            "COPY", '--chown=1000:1000 ["app.py", "/app/app.py"]', 3,
            'COPY --chown=1000:1000 ["app.py", "/app/app.py"]', 0,
        )
        self.assertEqual(optimizer.parse_copy_sources(instruction), ["app.py"])

    def test_nested_project_uses_its_own_build_context(self):
        repository = self.make_repo()
        service = repository / "service"
        service.mkdir()
        (service / "requirements.txt").write_text("requests==2.32.0\n", encoding="utf-8")
        (service / "app.py").write_text("print('service')\n", encoding="utf-8")
        (service / "Dockerfile").write_text(
            "FROM python:3.12-slim\nCOPY . /app\nRUN pip install -r /app/requirements.txt\n",
            encoding="utf-8",
        )
        run(repository, "git", "add", "service")
        run(repository, "git", "commit", "-qm", "add nested service")
        report = optimizer.analyze(service, service / "Dockerfile", 100)
        self.assertEqual(report["project_root"], str(service))
        self.assertEqual(report["layers"][0]["matched_files"], 2)
        self.assertIn("split-dependency-inputs", {item["kind"] for item in report["recommendations"]})

    def test_plain_progress_counts_dockerfile_steps_and_context(self):
        parser = build_observer.BuildProgressParser()
        lines = [
            "#1 [internal] load build definition from Dockerfile",
            "#1 DONE 0.0s",
            "#2 [1/3] FROM docker.io/library/python:3.12-slim",
            "#2 CACHED",
            "#3 [internal] load build context",
            "#3 transferring context: 2.45kB done",
            "#3 DONE 0.0s",
            "#4 [2/3] COPY . /app",
            "#4 DONE 0.2s",
            "#5 [3/3] RUN python -m compileall /app",
            "#5 ERROR: process failed with exit code 1",
        ]
        for line in lines:
            parser.feed(line)
        summary = parser.summary()
        self.assertEqual(
            (summary["total"], summary["cached"], summary["rebuilt"], summary["failed"], summary["incomplete"]),
            (3, 1, 1, 1, 0),
        )
        self.assertEqual(parser.context_bytes, 2450)
        self.assertEqual(summary["items"][1]["duration_seconds"], 0.2)
        self.assertIsNone(summary["items"][2]["duration_seconds"])

    def test_rawjson_progress_uses_cached_field_and_resolves_from(self):
        parser = build_observer.RawJsonProgressParser()
        events = [
            {"vertexes": [{"digest": "context", "name": "[internal] load build context"}]},
            {"statuses": [{"id": "transferring context:", "vertex": "context", "current": 4096}]},
            {"vertexes": [
                {"digest": "from", "name": "[1/3] FROM alpine", "started": "2026-01-01T00:00:00Z", "completed": "2026-01-01T00:00:00.01Z"},
                {"digest": "copy", "name": "[2/3] COPY app.py /app/", "started": "2026-01-01T00:00:00Z", "completed": "2026-01-01T00:00:00Z", "cached": True},
                {"digest": "run", "name": "[3/3] RUN python /app/app.py", "started": "2026-01-01T00:00:00Z", "completed": "2026-01-01T00:00:00.2Z"},
            ]},
        ]
        for event in events:
            parser.feed(json.dumps(event))
        summary = parser.summary()
        self.assertEqual(summary["progress_format"], "rawjson")
        self.assertEqual((summary["cached"], summary["rebuilt"], summary["resolved"]), (1, 1, 1))
        self.assertEqual(parser.context_bytes, 4096)
        self.assertEqual(summary["items"][2]["duration_seconds"], 0.2)

    def test_progress_history_does_not_persist_instruction_text(self):
        parser = build_observer.RawJsonProgressParser()
        secret = "DO_NOT_PERSIST_123"
        parser.feed(json.dumps({"vertexes": [{
            "digest": "run", "name": f"[1/1] RUN echo {secret}",
            "started": "2026-01-01T00:00:00Z", "completed": "2026-01-01T00:00:01Z",
        }]}))
        serialized = json.dumps(parser.summary())
        self.assertNotIn(secret, serialized)
        self.assertIn("instruction_sha256", serialized)

    def test_layer_comparison_handles_reuse_and_order(self):
        result = build_observer.compare_layers(["a", "b", "d"], ["a", "b", "c"])
        self.assertEqual(result["new"], 1)
        self.assertEqual(result["reused"], 2)
        self.assertEqual(result["removed"], 1)
        self.assertEqual(result["common_prefix"], 2)
        self.assertEqual(result["changed_positions"], 1)
        self.assertTrue(result["has_baseline"])

    def test_layer_comparison_separates_content_reuse_from_order(self):
        result = build_observer.compare_layers(["b", "a"], ["a", "b"])
        self.assertEqual(result["matching_diff_ids"], 2)
        self.assertEqual(result["unmatched_diff_ids"], 0)
        self.assertEqual(result["changed_positions"], 2)
        self.assertEqual(result["common_prefix"], 0)

    def test_snapshot_respects_dockerignore_and_detects_changes(self):
        root = Path(tempfile.mkdtemp())
        self.temporary_paths.append(root)
        (root / ".dockerignore").write_text("ignored.txt\ncache/\n", encoding="utf-8")
        (root / "app.py").write_text("one\n", encoding="utf-8")
        (root / "ignored.txt").write_text("secret\n", encoding="utf-8")
        (root / "cache").mkdir()
        (root / "cache" / "data.bin").write_bytes(b"ignored")
        first = build_observer.snapshot_context(root)
        self.assertIn("app.py", first)
        self.assertNotIn("ignored.txt", first)
        self.assertNotIn("cache/data.bin", first)
        (root / "app.py").write_text("two\n", encoding="utf-8")
        second = build_observer.snapshot_context(root)
        self.assertEqual(build_observer.changed_paths(second, first), ["app.py"])

    def test_snapshot_supports_negation_and_dockerfile_specific_ignore(self):
        root = Path(tempfile.mkdtemp())
        self.temporary_paths.append(root)
        (root / "Dockerfile.custom").write_text("FROM scratch\n", encoding="utf-8")
        (root / ".dockerignore").write_text("*.txt\n", encoding="utf-8")
        (root / "Dockerfile.custom.dockerignore").write_text("dist/\n!dist/keep.txt\n", encoding="utf-8")
        (root / "root.txt").write_text("included by specific rules\n", encoding="utf-8")
        (root / "dist").mkdir()
        (root / "dist" / "drop.txt").write_text("drop\n", encoding="utf-8")
        (root / "dist" / "keep.txt").write_text("keep\n", encoding="utf-8")
        snapshot = build_observer.snapshot_context(root, root / "Dockerfile.custom")
        self.assertIn("root.txt", snapshot)
        self.assertIn("dist/keep.txt", snapshot)
        self.assertNotIn("dist/drop.txt", snapshot)

    def test_first_layer_observation_is_a_baseline(self):
        result = build_observer.compare_layers(["a", "b"], None)
        self.assertEqual(result["new"], 2)
        self.assertFalse(result["has_baseline"])

    def test_analysis_excludes_dockerignored_paths(self):
        root = self.make_repo()
        (root / ".dockerignore").write_text("docs/\n", encoding="utf-8")
        (root / "docs").mkdir()
        (root / "docs" / "guide.md").write_text("one\n", encoding="utf-8")
        run(root, "git", "add", ".dockerignore", "docs/guide.md")
        run(root, "git", "commit", "-qm", "add docs")
        for index in range(3):
            (root / "docs" / "guide.md").write_text(f"docs {index}\n", encoding="utf-8")
            run(root, "git", "add", "docs/guide.md")
            run(root, "git", "commit", "-qm", f"docs {index}")
        report = optimizer.analyze(root, root / "Dockerfile", 100)
        layer = next(item for item in report["layers"] if item["sources"] == ["."])
        self.assertEqual(layer["matched_files"], 2)
        self.assertNotIn("docs", {item["area"] for item in report["volatile_areas"]})

    def test_measured_builds_feed_analysis_evidence(self):
        root = self.make_repo()
        optimizer.append_event(root, {
            "schema_version": 2,
            "kind": "build",
            "status": "success",
            "duration_seconds": 4.5,
            "context_bytes": 1234,
            "changed_paths": ["app.py"],
            "steps": {"total": 3, "cached": 2, "rebuilt": 1, "failed": 0, "incomplete": 0, "items": []},
            "image": {"new": 1, "reused": 2, "has_baseline": True},
        })
        report = optimizer.analyze(root, root / "Dockerfile", 100)
        evidence = report["evidence"]
        self.assertEqual(evidence["measured_builds"], 1)
        self.assertEqual(evidence["median_rebuilt_steps"], 1.0)
        self.assertEqual(evidence["median_unmatched_diff_ids"], 1.0)
        self.assertEqual(evidence["median_context_bytes"], 1234)
        self.assertIsNotNone(report["layers"][0]["local_likelihood"])

    def test_concurrent_event_appends_remain_valid(self):
        root = self.make_repo()
        count = 40
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(
                lambda index: optimizer.append_event(root, {"schema_version": 3, "kind": "task", "index": index}),
                range(count),
            ))
        events = optimizer.load_events(root, count + 1)
        self.assertEqual(len(events), count)
        self.assertEqual({event["index"] for event in events}, set(range(count)))

    def test_build_observer_records_and_compares_successful_builds(self):
        root = self.make_repo()
        args = optimizer.parser().parse_args([
            "build", "--root", str(root), "--tag", "example:test", "--quiet", "--progress-format", "plain",
        ])
        progress = [
            "#1 [1/2] FROM scratch\n", "#1 CACHED\n",
            "#2 [2/2] COPY . /app\n", "#2 DONE 0.1s\n",
        ]
        return_codes = iter([0, 1, 0])

        class FakeProcess:
            def __init__(self, command, **kwargs):
                self.command = command
                self.stdout = iter(progress)

            def wait(self):
                return next(return_codes)

        images = [
            {"id": "one", "size_bytes": 10, "repo_digests": [], "layer_diff_ids": ["a", "b"]},
            {"id": "two", "size_bytes": 11, "repo_digests": [], "layer_diff_ids": ["a", "c"]},
        ]
        with mock.patch.object(build_observer.subprocess, "Popen", FakeProcess), \
                mock.patch.object(build_observer, "inspect_image", side_effect=images), \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(build_observer.run_build(args, optimizer), 0)
            (root / "app.py").write_text("print('next')\n", encoding="utf-8")
            self.assertEqual(build_observer.run_build(args, optimizer), 1)
            (root / "requirements.txt").write_text("flask==3.1.0\n", encoding="utf-8")
            self.assertEqual(build_observer.run_build(args, optimizer), 0)

        events = optimizer.load_events(root)
        self.assertEqual(len(events), 3)
        self.assertEqual(events[0]["image"]["has_baseline"], False)
        self.assertEqual(events[1]["status"], "failure")
        self.assertEqual(events[1]["changed_paths"], ["app.py"])
        self.assertEqual(events[2]["changed_paths"], ["requirements.txt"])
        self.assertEqual(events[2]["image"]["new"], 1)
        self.assertEqual(events[2]["image"]["reused"], 1)
        self.assertNotIn("display", json.dumps(events))

    def test_inspection_failure_text_is_not_persisted(self):
        root = self.make_repo()
        args = optimizer.parser().parse_args([
            "build", "--root", str(root), "--tag", "example:test", "--quiet", "--progress-format", "plain",
        ])
        private_error = "registry response contained PRIVATE_DETAIL"

        class FakeProcess:
            stdout = iter(["#1 [1/1] FROM scratch\n", "#1 CACHED\n"])

            def __init__(self, command, **kwargs):
                pass

            def wait(self):
                return 0

        with mock.patch.object(build_observer.subprocess, "Popen", FakeProcess), \
                mock.patch.object(build_observer, "inspect_image", side_effect=RuntimeError(private_error)), \
                mock.patch("sys.stdout", new_callable=io.StringIO), \
                mock.patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(build_observer.run_build(args, optimizer), 0)
        serialized = json.dumps(optimizer.load_events(root))
        self.assertIn("local-image-inspection-failed", serialized)
        self.assertNotIn(private_error, serialized)

    def test_wendy_deployment_phase_tracker_separates_lifecycle(self):
        tracker = deployment_observer.DeploymentPhaseTracker("wendy")
        tracker.start(0.0)
        observations = [
            ("Building service app...", 0.1),
            ("#9 exporting to oci image format", 2.0),
            ("[apple-container] pushing image: example", 3.0),
            ("Pulling image on device...", 4.0),
            ("Unpack plan: 8 layers", 5.0),
            ("Creating container...", 6.0),
            ("Waiting for device:8080 to be ready...", 7.0),
            ("Ready.", 9.0),
        ]
        for line, timestamp in observations:
            tracker.feed(line, timestamp)
        result = tracker.finish(10.0)
        self.assertEqual(result["dominant_phase"], "readiness")
        self.assertEqual(result["phases"]["build"]["duration_seconds"], 1.9)
        self.assertEqual(result["phases"]["export"]["duration_seconds"], 1.0)
        self.assertEqual(result["phases"]["transfer"]["duration_seconds"], 2.0)
        self.assertEqual(result["phases"]["unpack"]["duration_seconds"], 1.0)
        self.assertEqual(result["phases"]["replacement"]["duration_seconds"], 1.0)
        self.assertEqual(result["phases"]["readiness"]["duration_seconds"], 3.0)
        self.assertEqual(result["unclassified_seconds"], 0.1)

    def test_custom_deployment_markers_validate_and_classify(self):
        markers = deployment_observer.parse_phase_markers(["build=^compile", "readiness=^healthy$"])
        tracker = deployment_observer.DeploymentPhaseTracker("generic", markers)
        tracker.start(0.0)
        tracker.feed("compile application", 1.0)
        tracker.feed("healthy", 4.0)
        result = tracker.finish(5.0)
        self.assertEqual(result["phases"]["build"]["duration_seconds"], 3.0)
        self.assertEqual(result["phases"]["readiness"]["duration_seconds"], 1.0)
        with self.assertRaises(ValueError):
            deployment_observer.parse_phase_markers(["unknown=marker"])
        with self.assertRaises(ValueError):
            deployment_observer.parse_phase_markers(["build=[invalid"])

    def test_deployment_adapter_detection_and_compose_markers(self):
        self.assertEqual(deployment_observer.infer_adapter(["wendy", "run"]), "wendy")
        self.assertEqual(deployment_observer.infer_adapter(["docker", "compose", "up"]), "compose")
        self.assertEqual(deployment_observer.infer_adapter(["docker-compose", "up"]), "compose")
        self.assertEqual(deployment_observer.infer_adapter(["custom-deploy"]), "generic")
        tracker = deployment_observer.DeploymentPhaseTracker("compose")
        tracker.start(0.0)
        tracker.feed("[+] Building 2.0s", 0.1)
        tracker.feed("#8 exporting to image", 2.0)
        tracker.feed("Container app Recreated", 3.0)
        tracker.feed("Container app Healthy", 4.0)
        result = tracker.finish(5.0)
        self.assertTrue(result["phases"]["build"]["observed"])
        self.assertTrue(result["phases"]["export"]["observed"])
        self.assertTrue(result["phases"]["replacement"]["observed"])
        self.assertTrue(result["phases"]["readiness"]["observed"])

    def test_deploy_command_records_phases_without_command_or_logs(self):
        root = self.make_repo()
        args = optimizer.parser().parse_args([
            "deploy", "--root", str(root), "--adapter", "wendy", "--target", "test-device",
            "--quiet", "--", "private-deploy-command", "--token", "PRIVATE_TOKEN",
        ])

        def fake_execute(command, command_root, tracker, quiet, json_output):
            self.assertEqual(command[0], "private-deploy-command")
            self.assertEqual(command_root, root.resolve())
            for line in (
                "Building service SECRET_BUILD_OUTPUT",
                "Pulling image on device...",
                "Unpack plan: 2 layers",
                "Creating container...",
                "Waiting for device:8080 to be ready...",
                "Ready.",
            ):
                tracker.feed(line, deployment_observer.time.monotonic())
            return 0

        clock = iter([0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0])
        with mock.patch.object(deployment_observer, "_execute", fake_execute), \
                mock.patch.object(deployment_observer.time, "monotonic", side_effect=lambda: next(clock)), \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(deployment_observer.run_deploy(args, optimizer), 0)

        event = optimizer.load_events(root)[-1]
        serialized = json.dumps(event)
        self.assertEqual(event["kind"], "deploy")
        self.assertEqual(event["status"], "success")
        self.assertEqual(event["deployment"]["adapter"], "wendy")
        self.assertEqual(event["deployment"]["dominant_phase"], "readiness")
        self.assertNotIn("PRIVATE_TOKEN", serialized)
        self.assertNotIn("SECRET_BUILD_OUTPUT", serialized)
        self.assertNotIn("private-deploy-command", serialized)

        report = optimizer.analyze(root, root / "Dockerfile", 100)
        self.assertEqual(report["evidence"]["measured_deployments"], 1)
        self.assertEqual(report["evidence"]["dominant_deployment_phase"], "readiness")
        self.assertIn("investigate-deploy-runtime", {item["kind"] for item in report["recommendations"]})

    def test_readiness_timeout_marks_successful_command_partial(self):
        tracker = deployment_observer.DeploymentPhaseTracker("wendy")
        tracker.start(0.0)
        tracker.feed("service error rate is zero", 0.5)
        tracker.feed("Waiting for device to be ready...", 1.0)
        tracker.feed("Warning: readiness probe timed out after 1m0s", 61.0)
        result = tracker.finish(62.0)
        self.assertIn("readiness-timeout", result["signals"])
        self.assertNotIn("deployment-failed", result["signals"])

    def test_deploy_command_propagates_failure_exit_code(self):
        root = self.make_repo()
        args = optimizer.parser().parse_args([
            "deploy", "--root", str(root), "--adapter", "generic", "--target", "failure-test",
            "--quiet", "--", "false-command",
        ])
        with mock.patch.object(deployment_observer, "_execute", return_value=7), \
                mock.patch.object(deployment_observer.time, "monotonic", side_effect=[1.0, 2.0]), \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(deployment_observer.run_deploy(args, optimizer), 7)
        event = optimizer.load_events(root)[-1]
        self.assertEqual(event["status"], "failure")
        self.assertEqual(event["deployment"]["exit_code"], 7)


if __name__ == "__main__":
    unittest.main()
