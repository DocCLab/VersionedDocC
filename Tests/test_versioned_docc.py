import importlib.util
import json
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
