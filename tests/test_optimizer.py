import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).parents[1] / "skills" / "optimize-docker-layers" / "scripts" / "docker_layer_optimizer.py"
SPEC = importlib.util.spec_from_file_location("docker_layer_optimizer", SCRIPT)
optimizer = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = optimizer
SPEC.loader.exec_module(optimizer)


def run(root: Path, *args: str) -> None:
    subprocess.run(args, cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


class OptimizerTests(unittest.TestCase):
    def make_repo(self) -> Path:
        root = Path(tempfile.mkdtemp())
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

    def test_record_stays_inside_git_metadata(self):
        root = self.make_repo()
        (root / "app.py").write_text("print('changed')\n", encoding="utf-8")
        args = optimizer.parser().parse_args([
            "record", "--root", str(root), "--kind", "task", "--from-git", "--tag", "Source Code",
        ])
        result = optimizer.record(args)
        state_file = Path(result["state_file"])
        self.assertTrue(state_file.is_file())
        self.assertIn(str(root / ".git"), str(state_file))
        event = json.loads(state_file.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(event["changed_paths"], ["app.py"])
        self.assertEqual(event["tags"], ["source-code"])

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
        self.assertEqual(report["layers"][0]["matched_files"], 3)
        self.assertIn("split-dependency-inputs", {item["kind"] for item in report["recommendations"]})


if __name__ == "__main__":
    unittest.main()
