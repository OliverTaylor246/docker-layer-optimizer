import dataclasses
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
import optimization_engine


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

    def test_optimize_plan_generates_manifest_first_patch_without_mutation(self):
        root = self.make_repo()
        before = (root / "Dockerfile").read_text(encoding="utf-8")
        result = optimization_engine.plan(root, root / "Dockerfile", optimizer)
        self.assertEqual(result["kind"], "optimization_plan")
        self.assertEqual(result["status"], "candidate")
        candidate = result["candidate"]
        self.assertEqual(candidate["kind"], "manifest-first")
        self.assertEqual(candidate["protected_changes"], ())
        self.assertIn('COPY ["requirements.txt", "/app/"]', candidate["patch"])
        self.assertEqual((root / "Dockerfile").read_text(encoding="utf-8"), before)
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory)
            optimization_engine._copy_tree(root, snapshot)
            optimization_engine._apply_patch(snapshot, candidate["patch"])
            rewritten = (snapshot / "Dockerfile").read_text(encoding="utf-8")
            self.assertLess(rewritten.index("COPY ["), rewritten.index("RUN pip install"))
            self.assertLess(rewritten.index("RUN pip install"), rewritten.index("COPY . /app"))

    def test_agent_candidate_detects_protected_and_out_of_scope_changes(self):
        root = self.make_repo()
        dockerfile = root / "Dockerfile"
        before = dockerfile.read_text(encoding="utf-8")
        after = before.replace("FROM python:3.12-slim", "FROM python:3.13-slim")
        patch = optimization_engine._unified_diff(Path("Dockerfile"), before, after)
        candidate = optimization_engine.candidate_from_patch(root, dockerfile, patch, optimizer)
        self.assertIn("protected-dockerfile-semantics", candidate.protected_changes)

        source_before = (root / "app.py").read_text(encoding="utf-8")
        source_patch = optimization_engine._unified_diff(
            Path("app.py"), source_before, source_before + "# faster\n",
        )
        source_candidate = optimization_engine.candidate_from_patch(root, dockerfile, source_patch, optimizer)
        self.assertTrue(any(value.startswith("outside-docker-build-scope:") for value in source_candidate.protected_changes))

    def test_benchmark_evaluation_requires_correctness_and_negative_controls(self):
        passing = lambda seconds: optimization_engine.BuildResult(0, seconds, 2, 1, 0)
        settings = optimization_engine.Settings(
            trials=3, min_relative_improvement=0.10, min_absolute_seconds=0.5, payback_deploys=20,
        )
        benchmark, gates = optimization_engine._evaluate(
            [passing(3.0), passing(3.1), passing(3.2)],
            [passing(1.0), passing(1.1), passing(1.2)],
            (passing(0.3), passing(0.4)),
            (passing(4.0), passing(4.1)),
            [True], settings, 5.0,
        )
        self.assertTrue(all(gates.values()))
        self.assertEqual(benchmark["source_change"]["absolute_improvement_seconds"], 2.0)
        _, missing_contract = optimization_engine._evaluate(
            [passing(3.0)] * 3, [passing(1.0)] * 3,
            (passing(0.3), passing(0.4)), (passing(4.0), passing(4.1)), [], settings, 5.0,
        )
        self.assertFalse(missing_contract["verification_contract_present"])
        self.assertFalse(missing_contract["verification_commands_passed"])

        noise_settings = dataclasses.replace(
            settings, min_absolute_seconds=0.25, max_absolute_regression_seconds=0.5,
        )
        _, noise_gates = optimization_engine._evaluate(
            [passing(2.0)] * 3, [passing(1.0)] * 3,
            (passing(0.2), passing(0.3)), (passing(1.5), passing(1.9)),
            [True], noise_settings, 5.0,
        )
        self.assertTrue(noise_gates["dependency_change_not_regressed"])

    def test_payback_precheck_skips_only_with_sufficient_history(self):
        settings = optimization_engine.Settings(trials=3, payback_deploys=20)
        shallow = {
            "evidence": {"measured_builds": 2, "median_duration_seconds": 10.0},
            "optimization_signal": {"max_change_likelihood": 0.5},
        }
        self.assertEqual(optimization_engine.payback_precheck(shallow, settings)["decision"], "insufficient-history")
        expensive = {
            "evidence": {"measured_builds": 3, "median_duration_seconds": 10.0},
            "optimization_signal": {"max_change_likelihood": 0.5},
        }
        estimate = optimization_engine.payback_precheck(expensive, settings)
        self.assertEqual(estimate["decision"], "skip")
        self.assertEqual(estimate["estimated_break_even_deploys"], 24.0)

    def test_verify_uses_snapshots_and_accepts_a_proven_candidate(self):
        root = self.make_repo()
        candidate = optimization_engine.generate_candidate(root, root / "Dockerfile", optimizer)
        self.assertIsNotNone(candidate)
        seen_roots = set()

        def fake_build(build_root, dockerfile, tag, settings, deadline):
            self.assertNotEqual(build_root, root)
            self.assertTrue(dockerfile.is_relative_to(build_root))
            seen_roots.add(build_root.name)
            dependency_changed = "dlo benchmark dependency" in (build_root / "requirements.txt").read_text()
            source_changed = "dlo benchmark source" in (build_root / "app.py").read_text()
            if dependency_changed:
                seconds = 4.0
            elif source_changed:
                seconds = 1.0 if build_root.name == "candidate" else 3.0
            else:
                seconds = 0.2
            return optimization_engine.BuildResult(0, seconds, 3, 1, 0)

        commands = []

        def fake_command(command, command_root, environment, deadline):
            commands.append(command)
            self.assertEqual(environment["DLO_PROJECT_ROOT"], str(command_root))
            return 0

        settings = optimization_engine.Settings(
            trials=3, budget_seconds=60, min_relative_improvement=0.10,
            min_absolute_seconds=0.5, payback_deploys=20,
            source_path="app.py", verification_commands=("python tests.py",),
        )
        verification = optimization_engine.verify(
            root, root / "Dockerfile", candidate, settings, optimizer,
            build_runner=fake_build, command_runner=fake_command,
        )
        self.assertTrue(verification["verified"])
        self.assertEqual(seen_roots, {"control", "candidate"})
        self.assertEqual(commands, ["python tests.py"])
        self.assertNotIn("dlo benchmark", (root / "app.py").read_text())

    def test_stale_preimage_blocks_application(self):
        root = self.make_repo()
        expected = optimization_engine._preimages(root, ["Dockerfile"])
        (root / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "candidate is stale"):
            optimization_engine._assert_preimages(root, expected)

    def test_correctness_failure_stops_before_benchmark_trials(self):
        root = self.make_repo()
        candidate = optimization_engine.generate_candidate(root, root / "Dockerfile", optimizer)
        builds = []

        def fake_build(build_root, dockerfile, tag, settings, deadline):
            builds.append(tag)
            return optimization_engine.BuildResult(0, 0.1, 2, 0, 0)

        settings = optimization_engine.Settings(
            source_path="app.py", verification_commands=("PRIVATE_FAILING_CHECK",),
        )
        with self.assertRaisesRegex(optimization_engine.VerificationFailure, "correctness-command-failed"):
            optimization_engine.verify(
                root, root / "Dockerfile", candidate, settings, optimizer,
                build_runner=fake_build,
                command_runner=lambda command, command_root, environment, deadline: 1,
            )
        self.assertEqual(len(builds), 2)

    def test_optimize_run_auto_applies_only_verified_candidate(self):
        root = self.make_repo()
        before = (root / "Dockerfile").read_text(encoding="utf-8")
        args = optimizer.parser().parse_args([
            "optimize", "--root", str(root), "--test", "PRIVATE_VERIFY_COMMAND", "--source-path", "app.py",
        ])

        def fake_verify(project_root, dockerfile, candidate, settings, module):
            return {
                "benchmark": {
                    "source_change": {
                        "control": {"median_seconds": 3.0}, "candidate": {"median_seconds": 1.0},
                        "absolute_improvement_seconds": 2.0, "relative_improvement": 0.6667,
                    },
                    "verification_seconds": 5.0, "estimated_break_even_deploys": 2.5,
                },
                "gates": {"all_test_gates": True}, "verified": True,
                "preimages": optimization_engine._preimages(root, candidate.affected_paths),
            }

        with mock.patch.object(optimization_engine, "verify", fake_verify):
            return_code, result = optimization_engine.run(args, optimizer)
        self.assertEqual(return_code, 0)
        self.assertTrue(result["applied"])
        after = (root / "Dockerfile").read_text(encoding="utf-8")
        self.assertNotEqual(after, before)
        self.assertLess(after.index("RUN pip install"), after.index("COPY . /app"))
        proof = json.loads(Path(result["proof_file"]).read_text(encoding="utf-8"))
        serialized = json.dumps(proof)
        self.assertNotIn("COPY . /app", serialized)
        self.assertNotIn("PRIVATE_VERIFY_COMMAND", serialized)
        self.assertNotIn("patch", proof)

    def test_proof_retention_prunes_expired_and_excess_records(self):
        root = self.make_repo()
        directory = optimization_engine._proof_directory(root, optimizer)
        directory.mkdir(parents=True)
        now = optimizer.dt.datetime.now(optimizer.dt.timezone.utc)
        for index in range(23):
            timestamp = now - optimizer.dt.timedelta(hours=index)
            value = {"timestamp": timestamp.isoformat(), "verified": True}
            (directory / f"recent-{index}.json").write_text(json.dumps(value), encoding="utf-8")
        expired = {"timestamp": (now - optimizer.dt.timedelta(days=31)).isoformat(), "verified": True}
        (directory / "expired.json").write_text(json.dumps(expired), encoding="utf-8")
        result = optimization_engine._prune_proofs(root, optimizer, now)
        self.assertEqual(result["retained"], 20)
        self.assertEqual(len(list(directory.glob("*.json"))), 20)


if __name__ == "__main__":
    unittest.main()
