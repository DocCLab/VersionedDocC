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

    def test_oci_cache_manifest_must_use_versioned_docc_artifact_type(self):
        manifest = {
            "artifactType": versioned_docc.OCI_ARTIFACT_TYPE,
            "config": {"digest": "sha256:" + "a" * 64},
        }
        result = mock.Mock(
            returncode=0,
            stdout=json.dumps(manifest),
            stderr="",
        )
        with mock.patch.object(
            versioned_docc, "run_status", return_value=result
        ) as run_status:
            self.assertTrue(
                versioned_docc.oci_artifact_exists(
                    "oras", "ghcr.io/example/cache", "tag"
                )
            )
        run_status.assert_called_once_with(
            ["oras", "manifest", "fetch", "ghcr.io/example/cache:tag"]
        )

        manifest["artifactType"] = "application/vnd.example.other"
        result.stdout = json.dumps(manifest)
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

    def test_api_changes_use_docc_canonical_url_for_overloaded_symbol(self):
        precise = "s:7DemoKit1PV6valueSivp"
        identifier = "doc://DemoKit/documentation/DemoKit/P/value-1a2b3"
        graph = {
            "symbols": [
                {
                    "identifier": {"precise": precise},
                    "pathComponents": ["P", "value"],
                    "names": {"title": "value"},
                }
            ]
        }
        document = {
            "identifier": {"url": identifier},
            "metadata": {"externalID": precise},
            "references": {
                identifier: {
                    "url": "/documentation/demokit/p/value-1a2b3",
                }
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph_path = root / "DemoKit.symbols.json"
            graph_path.write_text(json.dumps(graph), encoding="utf-8")
            documentation_root = root / "data" / "documentation"
            documentation_root.mkdir(parents=True)
            (documentation_root / "value-1a2b3.json").write_text(
                json.dumps(document), encoding="utf-8"
            )

            urls = api_changes.load_docc_urls(documentation_root)
            snapshot = api_changes.load_snapshot(graph_path, "demokit", urls)

        self.assertEqual(
            snapshot["P.value"]["path"],
            "/documentation/demokit/p/value-1a2b3",
        )

    def test_api_changes_omit_link_without_docc_canonical_url(self):
        graph = {
            "symbols": [
                {
                    "identifier": {"precise": "s:7DemoKit1PV5valueSivp"},
                    "pathComponents": ["P", "value"],
                    "names": {"title": "value"},
                }
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            graph_path = Path(directory) / "DemoKit.symbols.json"
            graph_path.write_text(json.dumps(graph), encoding="utf-8")
            snapshot = api_changes.load_snapshot(graph_path, "demokit", {})

        self.assertIsNone(snapshot["P.value"]["path"])

    def test_api_changes_merge_platform_snapshots_with_primary_declaration(self):
        def symbol(precise, title, declaration, platform):
            return {
                "identifier": {"precise": precise},
                "pathComponents": [title],
                "names": {"title": title},
                "kind": {"displayName": "Structure"},
                "declarationFragments": [{"spelling": declaration}],
                "availability": [
                    {
                        "domain": platform,
                        "introduced": {"major": 13 if platform == "iOS" else 10},
                    }
                ],
            }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ios = root / "ios.json"
            macos = root / "macos.json"
            ios.write_text(
                json.dumps(
                    {
                        "symbols": [
                            symbol("s:4Demo8CommonAPIV", "CommonAPI", "iOS primary", "iOS"),
                            symbol("s:4Demo10IOSOnlyAPIV", "IOSOnlyAPI", "iOS only", "iOS"),
                        ]
                    }
                ),
                encoding="utf-8",
            )
            macos.write_text(
                json.dumps(
                    {
                        "symbols": [
                            symbol(
                                "s:4Demo8CommonAPIV",
                                "CommonAPI",
                                "macOS secondary",
                                "macOS",
                            ),
                            symbol(
                                "s:4Demo12MacOSOnlyAPIV",
                                "MacOSOnlyAPI",
                                "macOS only",
                                "macOS",
                            ),
                        ]
                    }
                ),
                encoding="utf-8",
            )

            snapshot = api_changes.merge_snapshots([ios, macos], "demo")

        self.assertEqual(
            set(snapshot), {"CommonAPI", "IOSOnlyAPI", "MacOSOnlyAPI"}
        )
        self.assertEqual(snapshot["CommonAPI"]["declaration"], "iOS primary")
        self.assertEqual(
            snapshot["CommonAPI"]["availability"], ["iOS 13", "macOS 10"]
        )

    def test_api_changes_merge_deduplicates_reordered_overloads(self):
        def symbol(precise, declaration):
            return {
                "identifier": {"precise": precise},
                "pathComponents": ["make()"],
                "names": {"title": "make()"},
                "kind": {"displayName": "Function"},
                "declarationFragments": [{"spelling": declaration}],
            }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.json"
            second = root / "second.json"
            first.write_text(
                json.dumps(
                    {
                        "symbols": [
                            symbol("p1", "primary 1"),
                            symbol("p2", "primary 2"),
                        ]
                    }
                ),
                encoding="utf-8",
            )
            second.write_text(
                json.dumps(
                    {
                        "symbols": [
                            symbol("p2", "secondary 2"),
                            symbol("p1", "secondary 1"),
                        ]
                    }
                ),
                encoding="utf-8",
            )

            snapshot = api_changes.merge_snapshots([first, second], "demo")

        self.assertEqual(len(snapshot), 2)
        self.assertEqual({item["precise"] for item in snapshot.values()}, {"p1", "p2"})
        self.assertEqual(
            {item["declaration"] for item in snapshot.values()},
            {"primary 1", "primary 2"},
        )

    def test_api_changes_dashboard_restores_controls_from_url(self):
        arguments = mock.Mock(
            hosting_base_path="/DemoKit",
            default_version="main",
            module_path="demokit",
            project_name="DemoKit",
            build_date="2026-08-05",
            page_size=17,
        )
        comparison = {
            "id": "0.1.0-to-main",
            "previousVersion": "0.1.0",
            "currentVersion": "main",
            "counts": {"added": 0, "modified": 0, "removed": 0},
            "changes": [],
        }

        dashboard = api_changes.render_dashboard([comparison], arguments)

        self.assertIn("function restoreState()", dashboard)
        self.assertIn("function persistState()", dashboard)
        self.assertIn("parameters.get('compare')", dashboard)
        self.assertIn("parameters.get('show')", dashboard)
        self.assertIn("parameters.get('search')", dashboard)
        self.assertIn("const PAGE_SIZE=17", dashboard)
        self.assertIn("window.addEventListener('pageshow',restoreAndRender)", dashboard)
        self.assertIn("window.addEventListener('popstate',restoreAndRender)", dashboard)

    def test_api_changes_page_size_defaults_and_accepts_business_configuration(self):
        config = {
            "schemaVersion": 1,
            "projectName": "DemoKit",
            "moduleName": "DemoKit",
            "targetName": "DemoKit",
            "catalogPath": "Sources/DemoKit/DemoKit.docc",
            "hostingBasePath": "/DemoKit",
            "versions": [
                {"name": "main", "ref": "HEAD"},
                {"name": "0.1.0", "ref": "0.1.0"},
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / ".vdc.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            loaded, _ = versioned_docc.load_config(root, path)
            self.assertEqual(loaded["apiChanges"]["pageSize"], 10)

            config["apiChanges"] = {"pageSize": 25}
            path.write_text(json.dumps(config), encoding="utf-8")
            loaded, _ = versioned_docc.load_config(root, path)
            self.assertEqual(loaded["apiChanges"]["pageSize"], 25)

    def test_symbol_graph_platform_configuration_orders_default_first(self):
        config = {
            "schemaVersion": 1,
            "projectName": "DemoKit",
            "moduleName": "DemoKit",
            "targetName": "DemoKit",
            "catalogPath": "Sources/DemoKit/DemoKit.docc",
            "hostingBasePath": "/DemoKit",
            "versions": [
                {"name": "main", "ref": "HEAD"},
                {"name": "0.1.0", "ref": "0.1.0"},
            ],
            "symbolGraph": {
                "emitExtensionBlocks": True,
                "defaultPlatform": "iOS",
                "platforms": [
                    {
                        "name": "macOS",
                        "triple": "arm64-apple-macosx",
                        "sdk": "macosx",
                    },
                    {
                        "name": "iOS",
                        "triple": "arm64-apple-ios",
                        "sdk": "iphoneos",
                    },
                ],
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / ".vdc.json"
            path.write_text(json.dumps(config), encoding="utf-8")

            loaded, _ = versioned_docc.load_config(root, path)

        platforms = loaded["symbolGraph"]["platforms"]
        self.assertEqual([platform["name"] for platform in platforms], ["iOS", "macOS"])
        self.assertEqual(platforms[0]["buildArguments"], [])
        self.assertTrue(loaded["symbolGraph"]["emitExtensionBlocks"])

    def test_symbol_graph_default_platform_must_be_configured(self):
        config = {
            "schemaVersion": 1,
            "projectName": "DemoKit",
            "moduleName": "DemoKit",
            "targetName": "DemoKit",
            "catalogPath": "Sources/DemoKit/DemoKit.docc",
            "hostingBasePath": "/DemoKit",
            "versions": [
                {"name": "main", "ref": "HEAD"},
                {"name": "0.1.0", "ref": "0.1.0"},
            ],
            "symbolGraph": {
                "defaultPlatform": "visionOS",
                "platforms": [
                    {"name": "iOS", "triple": "arm64-apple-ios"},
                ],
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / ".vdc.json"
            path.write_text(json.dumps(config), encoding="utf-8")

            with self.assertRaisesRegex(
                versioned_docc.VersionedDocCError, "defaultPlatform"
            ):
                versioned_docc.load_config(root, path)

    def test_historical_catalog_fallback_only_accepts_current(self):
        config = {
            "schemaVersion": 1,
            "projectName": "DemoKit",
            "moduleName": "DemoKit",
            "targetName": "DemoKit",
            "catalogPath": "Sources/DemoKit/DemoKit.docc",
            "hostingBasePath": "/DemoKit",
            "versions": [
                {"name": "main", "ref": "HEAD"},
                {"name": "0.1.0", "ref": "0.1.0"},
            ],
            "historicalCatalogFallback": "minimal",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / ".vdc.json"
            path.write_text(json.dumps(config), encoding="utf-8")

            with self.assertRaisesRegex(
                versioned_docc.VersionedDocCError,
                "historicalCatalogFallback",
            ):
                versioned_docc.load_config(root, path)

    def test_default_configuration_filename_is_vdc_json(self):
        with mock.patch.object(
            versioned_docc.sys, "argv", ["versioned-docc", "build"]
        ):
            build = versioned_docc.parse_arguments()
        with mock.patch.object(
            versioned_docc.sys, "argv", ["versioned-docc", "preview"]
        ):
            preview = versioned_docc.parse_arguments()

        self.assertEqual(build.config, ".vdc.json")
        self.assertEqual(preview.config, ".vdc.json")

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

    def test_retain_symbol_graph_module_keeps_extension_graphs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixtures = {
                "OpenSwiftUI.symbols.json": "OpenSwiftUI",
                "OpenSwiftUI@Foundation.symbols.json": "OpenSwiftUI",
                "SwiftSyntax.symbols.json": "SwiftSyntax",
            }
            for name, module in fixtures.items():
                (root / name).write_text(
                    json.dumps({"module": {"name": module}}), encoding="utf-8"
                )

            retained = versioned_docc.retain_symbol_graph_module(root, "OpenSwiftUI")

            self.assertEqual(
                [path.name for path in retained],
                ["OpenSwiftUI.symbols.json", "OpenSwiftUI@Foundation.symbols.json"],
            )
            self.assertFalse((root / "SwiftSyntax.symbols.json").exists())

    def test_module_symbol_graph_paths_include_extension_graphs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            platform = root / "00-ios"
            platform.mkdir()
            expected = [
                platform / "ScreenShieldKit.symbols.json",
                platform / "ScreenShieldKit@UIKit.symbols.json",
            ]
            for path in [*expected, platform / "SwiftSyntax.symbols.json"]:
                path.write_text("{}\n", encoding="utf-8")

            paths = versioned_docc.module_symbol_graph_paths(
                root, "ScreenShieldKit"
            )

        self.assertEqual(paths, expected)

    def test_cache_validation_accepts_nested_platform_symbol_graphs(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            (cache / "symbols" / "00-ios").mkdir(parents=True)
            (cache / "site").mkdir()
            (cache / "symbols" / "00-ios" / "DemoKit.symbols.json").write_text(
                "{}\n", encoding="utf-8"
            )
            (cache / "site" / "index.html").write_text("fixture\n", encoding="utf-8")
            (cache / "metadata.json").write_text(
                json.dumps({"sourceCommit": "commit", "buildFingerprint": "fingerprint"}),
                encoding="utf-8",
            )

            self.assertTrue(
                versioned_docc.cache_valid(
                    cache, "commit", "fingerprint", "DemoKit"
                )
            )

    def test_prune_site_to_module_removes_dependency_documentation_and_indexes(self):
        with tempfile.TemporaryDirectory() as directory:
            site = Path(directory)
            for path in (
                site / "documentation" / "openswiftui" / "index.html",
                site / "documentation" / "uikit" / "index.html",
                site / "documentation" / "swiftsyntax" / "index.html",
                site / "data" / "documentation" / "openswiftui" / "anchor.json",
                site / "data" / "documentation" / "uikit" / "view.json",
                site / "data" / "documentation" / "swiftsyntax" / "token.json",
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture\n", encoding="utf-8")
            for name in ("openswiftui.json", "uikit.json", "swiftsyntax.json"):
                (site / "data" / "documentation" / name).write_text(
                    "{}\n", encoding="utf-8"
                )

            index_path = site / "index" / "index.json"
            index_path.parent.mkdir()
            index_path.write_text(
                json.dumps(
                    {
                        "interfaceLanguages": {
                            "swift": [
                                {"path": "/documentation/openswiftui"},
                                {"path": "/documentation/uikit"},
                                {"path": "/documentation/swiftsyntax"},
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            indexing_records = [
                {
                    "location": {
                        "reference": {
                            "url": "doc://OpenSwiftUI/documentation/OpenSwiftUI/Anchor"
                        }
                    }
                },
                {
                    "location": {
                        "reference": {
                            "url": "doc://OpenSwiftUI/documentation/UIKit/UIView"
                        }
                    }
                },
                {
                    "location": {
                        "reference": {
                            "url": "doc://OpenSwiftUI/documentation/SwiftSyntax/Token"
                        }
                    }
                },
                {
                    "location": {
                        "reference": {"url": "doc://OpenSwiftUI/tutorials/OpenSwiftUI"}
                    }
                },
            ]
            (site / "indexing-records.json").write_text(
                json.dumps(indexing_records), encoding="utf-8"
            )
            entities = [
                {
                    "referenceURL": "doc://OpenSwiftUI/documentation/OpenSwiftUI/Anchor"
                },
                {
                    "referenceURL": "doc://OpenSwiftUI/documentation/UIKit/UIView"
                },
                {
                    "referenceURL": "doc://OpenSwiftUI/documentation/SwiftSyntax/Token"
                },
            ]
            (site / "linkable-entities.json").write_text(
                json.dumps(entities), encoding="utf-8"
            )

            versioned_docc.prune_site_to_module(
                site, "openswiftui", ["UIKit"]
            )

            self.assertTrue((site / "documentation" / "openswiftui").is_dir())
            self.assertTrue((site / "documentation" / "uikit").is_dir())
            self.assertFalse((site / "documentation" / "swiftsyntax").exists())
            self.assertTrue((site / "data" / "documentation" / "openswiftui").is_dir())
            self.assertTrue((site / "data" / "documentation" / "uikit").is_dir())
            self.assertTrue((site / "data" / "documentation" / "openswiftui.json").is_file())
            self.assertTrue((site / "data" / "documentation" / "uikit.json").is_file())
            self.assertFalse((site / "data" / "documentation" / "swiftsyntax").exists())
            self.assertFalse((site / "data" / "documentation" / "swiftsyntax.json").exists())
            self.assertEqual(
                json.loads(index_path.read_text())["interfaceLanguages"]["swift"],
                [
                    {"path": "/documentation/openswiftui"},
                    {"path": "/documentation/uikit"},
                ],
            )
            self.assertEqual(
                json.loads((site / "indexing-records.json").read_text()),
                [indexing_records[0], indexing_records[1], indexing_records[3]],
            )
            self.assertEqual(
                json.loads((site / "linkable-entities.json").read_text()),
                [entities[0], entities[1]],
            )


if __name__ == "__main__":
    unittest.main()
