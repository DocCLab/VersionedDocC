import importlib.util
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


RESOURCE_ROOT = Path(__file__).parents[1] / "Sources" / "VersionedDocC" / "Resources"


def load_module(name):
    specification = importlib.util.spec_from_file_location(name, RESOURCE_ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


versioned_docc = load_module("versioned_docc")
api_changes = load_module("api_changes")


class VersionedDocCTests(unittest.TestCase):
    def test_oci_cache_tag_is_content_specific(self):
        version = {"name": "0.20.1", "ref": "0.20.1"}
        first = versioned_docc.oci_cache_tag(version, "a" * 40, "b" * 64)

        self.assertEqual(first, versioned_docc.oci_cache_tag(version, "a" * 40, "b" * 64))
        self.assertNotEqual(first, versioned_docc.oci_cache_tag(version, "c" * 40, "b" * 64))
        self.assertNotEqual(first, versioned_docc.oci_cache_tag(version, "a" * 40, "d" * 64))
        self.assertRegex(first, r"^cache-0\.20\.1-[0-9a-f]{32}$")

    def test_oci_cache_excludes_development_by_default(self):
        config = {
            "releasePolicy": {
                "development": {"name": "main", "ref": "HEAD"},
            },
            "ociCache": {"repository": "ghcr.io/example/cache"},
        }

        self.assertFalse(
            versioned_docc.uses_oci_cache(config, {"name": "main", "ref": "HEAD"})
        )
        self.assertTrue(
            versioned_docc.uses_oci_cache(
                config, {"name": "0.20.1", "ref": "0.20.1"}
            )
        )

        config["ociCache"]["includeDevelopment"] = True
        self.assertTrue(
            versioned_docc.uses_oci_cache(config, {"name": "main", "ref": "HEAD"})
        )

    def test_oci_cache_archive_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache_entry = root / "cache"
            (cache_entry / "site").mkdir(parents=True)
            (cache_entry / "symbols").mkdir()
            (cache_entry / "metadata.json").write_text('{"schemaVersion": 1}\n')
            (cache_entry / "site" / "index.html").write_text("<h1>Demo</h1>\n")
            (cache_entry / "symbols" / "DemoKit.symbols.json").write_text("{}\n")
            archive = root / "cache.tar.gz"
            restored = root / "restored"

            versioned_docc.create_cache_archive(cache_entry, archive)
            versioned_docc.extract_cache_archive(archive, restored)

            self.assertEqual(
                (restored / "site" / "index.html").read_text(),
                "<h1>Demo</h1>\n",
            )
            self.assertEqual(
                (restored / "symbols" / "DemoKit.symbols.json").read_text(),
                "{}\n",
            )

    def test_oci_cache_archive_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root / "malicious.tar.gz"
            with versioned_docc.tarfile.open(archive_path, mode="w:gz") as archive:
                information = versioned_docc.tarfile.TarInfo("../outside")
                information.size = 4
                archive.addfile(information, io.BytesIO(b"nope"))

            with self.assertRaisesRegex(versioned_docc.VersionedDocCError, "unsafe OCI"):
                versioned_docc.extract_cache_archive(archive_path, root / "restored")
            self.assertFalse((root / "outside").exists())

    def test_oci_cache_metadata_must_match_requested_build(self):
        version = {"name": "0.20.1", "ref": "0.20.1"}
        metadata = {
            "version": "0.20.1",
            "sourceCommit": "a" * 40,
            "buildFingerprint": "b" * 64,
        }
        with mock.patch.object(versioned_docc, "run", return_value=json.dumps(metadata)):
            restored = versioned_docc.validate_oci_metadata(
                "oras",
                "ghcr.io/example/cache",
                "tag",
                version,
                "a" * 40,
                "b" * 64,
            )
        self.assertEqual(restored, metadata)

        metadata["sourceCommit"] = "c" * 40
        with (
            mock.patch.object(versioned_docc, "run", return_value=json.dumps(metadata)),
            self.assertRaisesRegex(
                versioned_docc.VersionedDocCError, "sourceCommit"
            ),
        ):
            versioned_docc.validate_oci_metadata(
                "oras",
                "ghcr.io/example/cache",
                "tag",
                version,
                "a" * 40,
                "b" * 64,
            )

    def test_oci_cache_descriptor_must_use_versioned_docc_artifact_type(self):
        descriptor = {
            "artifactType": versioned_docc.OCI_ARTIFACT_TYPE,
            "digest": "sha256:" + "a" * 64,
        }
        result = mock.Mock(
            returncode=0,
            stdout=json.dumps(descriptor),
            stderr="",
        )
        with mock.patch.object(versioned_docc, "run_status", return_value=result):
            self.assertTrue(
                versioned_docc.oci_artifact_exists(
                    "oras", "ghcr.io/example/cache", "tag"
                )
            )

        descriptor["artifactType"] = "application/vnd.example.other"
        result.stdout = json.dumps(descriptor)
        with (
            mock.patch.object(versioned_docc, "run_status", return_value=result),
            self.assertRaisesRegex(
                versioned_docc.VersionedDocCError, "unexpected OCI artifact type"
            ),
        ):
            versioned_docc.oci_artifact_exists(
                "oras", "ghcr.io/example/cache", "tag"
            )

    def test_release_policy_combines_latest_and_pinned_versions(self):
        config = {
            "defaultVersion": "main",
            "releasePolicy": {
                "latest": 1,
                "development": {"name": "main", "ref": "HEAD"},
                "pinned": ["0.19.0"],
            },
        }
        with mock.patch.object(versioned_docc, "semantic_versions", return_value=["0.20.1"]):
            versions = versioned_docc.configured_versions(Path("/unused"), config)

        self.assertEqual(
            versions,
            [
                {"name": "main", "ref": "HEAD"},
                {"name": "0.20.1", "ref": "0.20.1"},
                {"name": "0.19.0", "ref": "0.19.0"},
            ],
        )

    def test_release_policy_deduplicates_latest_pinned_version(self):
        config = {
            "defaultVersion": "main",
            "releasePolicy": {
                "latest": 1,
                "pinned": ["0.20.1"],
            },
        }
        with mock.patch.object(versioned_docc, "semantic_versions", return_value=["0.20.1"]):
            versions = versioned_docc.configured_versions(Path("/unused"), config)

        self.assertEqual(
            versions,
            [
                {"name": "main", "ref": "HEAD"},
                {"name": "0.20.1", "ref": "0.20.1"},
            ],
        )

    def test_tool_release_does_not_invalidate_build_cache(self):
        config = {
            "targetName": "DemoKit",
            "moduleName": "DemoKit",
            "catalogPath": "Sources/DemoKit/DemoKit.docc",
            "environment": {},
            "buildArguments": [],
            "doccArguments": [],
            "symbolGraph": {},
            "allowedModules": ["DemoKit"],
        }
        with (
            mock.patch.object(versioned_docc, "run", return_value="Swift fixture"),
            mock.patch.object(versioned_docc, "sha256_file", return_value="docc fixture"),
        ):
            before = versioned_docc.build_fingerprint(config, Path("/docc"), "header")
            with mock.patch.object(versioned_docc, "VERSION", "99.0.0"):
                after = versioned_docc.build_fingerprint(config, Path("/docc"), "header")

        self.assertEqual(before, after)

    def test_github_pages_fallback_redirects_legacy_path_to_default_version(self):
        config = {
            "hostingBasePath": "/DemoKit",
            "defaultVersion": "main",
            "modulePath": "demokit",
            "projectName": "DemoKit",
        }
        fallback = versioned_docc.github_pages_fallback(config)

        self.assertIn('const legacyRoot = "/DemoKit/documentation";', fallback)
        self.assertIn('const versionedRoot = "/DemoKit/main/documentation";', fallback)
        self.assertIn("window.location.search + window.location.hash", fallback)
        self.assertIn("window.location.replace(target)", fallback)
        self.assertIn("data-versioned-docc-pages-fallback", fallback)

    def test_legacy_routing_files_cover_project_and_deploy_roots(self):
        config = {
            "hostingBasePath": "/DemoKit",
            "defaultVersion": "main",
            "modulePath": "demokit",
            "projectName": "DemoKit",
        }
        with tempfile.TemporaryDirectory() as directory:
            deploy_root = Path(directory)
            output_path = deploy_root / "DemoKit"
            output_path.mkdir()
            versioned_docc.write_legacy_routing_files(output_path, config)

            for root in (deploy_root, output_path):
                self.assertTrue((root / "404.html").is_file())
                self.assertEqual(
                    (root / "_redirects").read_text(),
                    "/DemoKit/documentation/* "
                    "/DemoKit/main/documentation/:splat 301\n",
                )

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
                subprocess.run(
                    ["git", "-C", str(repository), "config", "commit.gpgsign", "false"],
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
