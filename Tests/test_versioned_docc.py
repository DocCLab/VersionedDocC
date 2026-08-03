import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


RESOURCE_ROOT = Path(__file__).parents[1] / "Sources" / "VersionedDocC" / "Resources"


def load_module(name):
    specification = importlib.util.spec_from_file_location(name, RESOURCE_ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


versioned_docc = load_module("versioned_docc")
api_changes = load_module("api_changes")


class VersionedDocCTests(unittest.TestCase):
    def test_prepared_source_uses_clones_without_registering_worktrees(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dependency = root / "Dependency"
            package = root / "Package"
            for repository in (dependency, package):
                subprocess.run(["git", "init", "-q", str(repository)], check=True)
                subprocess.run(
                    ["git", "-C", str(repository), "config", "user.email", "test@example.com"],
                    check=True,
                )
                subprocess.run(
                    ["git", "-C", str(repository), "config", "user.name", "VersionedDocC Tests"],
                    check=True,
                )

            (dependency / "Dependency.swift").write_text("public struct Dependency {}\n")
            subprocess.run(["git", "-C", str(dependency), "add", "."], check=True)
            subprocess.run(["git", "-C", str(dependency), "commit", "-qm", "Initial"], check=True)
            dependency_revision = versioned_docc.git(dependency, "rev-parse", "HEAD")

            (package / "Package.resolved").write_text(
                json.dumps(
                    {
                        "pins": [
                            {
                                "identity": "dependency",
                                "state": {"revision": dependency_revision},
                            }
                        ]
                    }
                )
            )
            (package / "Package.swift").write_text("// fixture\n")
            subprocess.run(["git", "-C", str(package), "add", "."], check=True)
            subprocess.run(["git", "-C", str(package), "commit", "-qm", "Initial"], check=True)
            package_revision = versioned_docc.git(package, "rev-parse", "HEAD")
            before = versioned_docc.git(package, "worktree", "list", "--porcelain")

            config = {"localDependencies": {"dependency": str(dependency)}}
            version = {"name": "fixture", "ref": package_revision}
            with versioned_docc.PreparedSource(package, config, version, package_revision) as source:
                self.assertTrue((source / ".git").is_dir())
                self.assertTrue((source.parent / "Dependency" / "Dependency.swift").is_file())

            after = versioned_docc.git(package, "worktree", "list", "--porcelain")
            self.assertEqual(before, after)

    def test_protocol_extension_diff_has_one_canonical_addition(self):
        canonical = {
            "P.accentColor": {
                "id": "P.accentColor",
                "title": "accentColor",
                "displayId": "P.accentColor",
                "declaration": "var accentColor: Int { get }",
                "kind": "Instance Property",
                "availability": [],
            }
        }
        comparison = api_changes.compare("0.0.0", "main", {}, canonical)
        self.assertEqual(comparison["counts"], {"added": 1, "modified": 0, "removed": 0})
        self.assertEqual(comparison["changes"][0]["current"]["displayId"], "P.accentColor")

    def test_filter_symbol_graph_removes_external_modules(self):
        graph = {
            "symbols": [
                {"identifier": {"precise": "s:4Demo1PV"}},
                {"identifier": {"precise": "s:5Other1QV"}},
            ],
            "relationships": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Demo.symbols.json"
            path.write_text(json.dumps(graph), encoding="utf-8")
            versioned_docc.filter_symbol_graph(path, {"Demo"})
            filtered = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(
            [item["identifier"]["precise"] for item in filtered["symbols"]],
            ["s:4Demo1PV"],
        )


if __name__ == "__main__":
    unittest.main()
