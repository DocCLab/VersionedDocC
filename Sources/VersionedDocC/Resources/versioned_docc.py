#!/usr/bin/env python3

import argparse
import datetime as dt
import gzip
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit


VERSION = "0.0.19"
DEFAULT_CONFIG = ".vdc.json"
# Keep this stable across releases that only change assembly, routing, or the
# command interface. Bump it only when the per-version DocC cache contents must
# be regenerated. Its initial value preserves 0.0.1 cache fingerprints.
BUILD_CACHE_REVISION = "0.0.5"
SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")
LATEST_RELEASE_STRATEGIES = {"majorMinor", "semanticVersion", "tagDate"}
OPTIONS_TOKEN = "__VERSIONED_DOCC_VERSION_OPTIONS__"
OCI_ARTIFACT_TYPE = "application/vnd.openswiftuiproject.versioned-docc.cache.v1"
OCI_CONFIG_TYPE = "application/vnd.openswiftuiproject.versioned-docc.cache.config.v1+json"
OCI_LAYER_TYPE = "application/vnd.openswiftuiproject.versioned-docc.cache.layer.v1.tar+gzip"
OCI_ARCHIVE_NAME = "versioned-docc-cache.tar.gz"
VERSIONED_DOCC_REPOSITORY = "https://github.com/DocCLab/VersionedDocC"


class VersionedDocCError(RuntimeError):
    pass


def default_build_date():
    return dt.datetime.now(dt.timezone.utc).date().isoformat()


def run(command, cwd=None, environment=None, capture=False, log_path=None):
    merged_environment = os.environ.copy()
    if environment:
        merged_environment.update({key: str(value) for key, value in environment.items()})
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("w", encoding="utf-8") as log:
            result = subprocess.run(
                command,
                cwd=cwd,
                env=merged_environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
            )
        if result.returncode:
            tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-120:]
            raise VersionedDocCError(
                f"command failed ({result.returncode}): {' '.join(map(str, command))}\n"
                + "\n".join(tail)
            )
        return ""
    result = subprocess.run(
        command,
        cwd=cwd,
        env=merged_environment,
        capture_output=capture,
        text=True,
    )
    if result.returncode:
        details = (result.stderr or result.stdout or "").strip()
        raise VersionedDocCError(
            f"command failed ({result.returncode}): {' '.join(map(str, command))}"
            + (f"\n{details}" if details else "")
        )
    return result.stdout.strip() if capture else ""


def run_status(command, cwd=None, environment=None):
    merged_environment = os.environ.copy()
    if environment:
        merged_environment.update({key: str(value) for key, value in environment.items()})
    return subprocess.run(
        command,
        cwd=cwd,
        env=merged_environment,
        capture_output=True,
        text=True,
    )


def git(repository, *arguments):
    return run(["git", "-C", str(repository), *arguments], capture=True)


def resolve_path(root, value):
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def ensure_safe_child(path, parent, label):
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError as error:
        raise VersionedDocCError(f"unsafe {label} outside {parent}: {path}") from error
    if path.resolve() == parent.resolve():
        raise VersionedDocCError(f"refusing {label} at protected root: {path}")


def remove_tree(path, parent, label):
    if not path.exists():
        return
    ensure_safe_child(path, parent, label)
    shutil.rmtree(path)


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_config(package_root, config_path):
    path = resolve_path(package_root, config_path)
    if not path.is_file():
        raise VersionedDocCError(f"missing configuration file: {path}")
    with path.open(encoding="utf-8") as source:
        config = json.load(source)
    if config.get("schemaVersion") != 1:
        raise VersionedDocCError("configuration schemaVersion must be 1")
    documentation_only = config.setdefault("documentationOnly", False)
    if not isinstance(documentation_only, bool):
        raise VersionedDocCError("documentationOnly must be a boolean")
    required = ["projectName", "catalogPath", "hostingBasePath"]
    required.extend(
        ["modulePath"] if documentation_only else ["moduleName", "targetName"]
    )
    missing = [key for key in required if not config.get(key)]
    if missing:
        raise VersionedDocCError(f"missing configuration keys: {', '.join(missing)}")
    configured_base_path = config["hostingBasePath"]
    if not isinstance(configured_base_path, str):
        raise VersionedDocCError(
            f"invalid hostingBasePath: {configured_base_path}"
        )
    base_path = "" if configured_base_path == "/" else (
        "/" + configured_base_path.strip("/")
    )
    if base_path and not re.fullmatch(r"(?:/[A-Za-z0-9._-]+)+", base_path):
        raise VersionedDocCError(f"invalid hostingBasePath: {base_path}")
    config["hostingBasePath"] = base_path
    if not documentation_only:
        config.setdefault("modulePath", config["moduleName"].lower())
    config.setdefault("defaultVersion", "main")
    config.setdefault("outputPath", f".docs/build/versioned-site{base_path}")
    config.setdefault("cachePath", ".docs/cache/versioned-docc")
    config.setdefault("buildArguments", ["--disable-index-store"])
    config.setdefault("doccArguments", ["--emit-digest"])
    config.setdefault("environment", {})
    config.setdefault("localDependencies", {})
    additional_modules = config.setdefault("additionalModules", [])
    if not isinstance(additional_modules, list):
        raise VersionedDocCError("additionalModules must be an array")
    if documentation_only and additional_modules:
        raise VersionedDocCError(
            "additionalModules cannot be used with documentationOnly"
        )
    module_names = {config.get("moduleName", "").casefold()}
    module_paths = {config["modulePath"].casefold()}
    for index, module in enumerate(additional_modules):
        label = f"additionalModules[{index}]"
        if not isinstance(module, dict):
            raise VersionedDocCError(f"{label} must be an object")
        module_name = module.get("moduleName")
        symbol_graph_path = module.get("symbolGraphPath")
        if not isinstance(module_name, str) or not module_name:
            raise VersionedDocCError(f"{label}.moduleName is required")
        if not isinstance(symbol_graph_path, str) or not symbol_graph_path:
            raise VersionedDocCError(f"{label}.symbolGraphPath is required")
        module_path = module.setdefault("modulePath", module_name.lower())
        if not isinstance(module_path, str) or not module_path:
            raise VersionedDocCError(f"{label}.modulePath must be a non-empty string")
        catalog_path = module.get("catalogPath")
        if catalog_path is not None and (
            not isinstance(catalog_path, str) or not catalog_path
        ):
            raise VersionedDocCError(f"{label}.catalogPath must be a non-empty string")
        source_repository = module.get("sourceRepository")
        source_root = module.get("sourceRoot")
        if (source_repository is None) != (source_root is None):
            raise VersionedDocCError(
                f"{label}.sourceRepository and {label}.sourceRoot must be configured together"
            )
        if source_repository is not None and (
            not isinstance(source_repository, str) or not source_repository
        ):
            raise VersionedDocCError(
                f"{label}.sourceRepository must be a non-empty string"
            )
        if source_root is not None and (
            not isinstance(source_root, str) or not source_root
        ):
            raise VersionedDocCError(
                f"{label}.sourceRoot must be a non-empty string"
            )
        if "versions" in module:
            raise VersionedDocCError(
                f"{label}.versions is not supported; additional modules follow "
                "every documentation version"
            )
        if module_name.casefold() in module_names:
            raise VersionedDocCError(f"duplicate moduleName: {module_name}")
        if module_path.casefold() in module_paths:
            raise VersionedDocCError(f"duplicate modulePath: {module_path}")
        module_names.add(module_name.casefold())
        module_paths.add(module_path.casefold())
    config.setdefault(
        "allowedModules",
        (
            [config["modulePath"]]
            if documentation_only
            else [
                config["moduleName"],
                *[module["moduleName"] for module in additional_modules],
            ]
        ),
    )
    allowed_module_names = {
        module.casefold() for module in config["allowedModules"]
    }
    for module in additional_modules:
        if module["moduleName"].casefold() not in allowed_module_names:
            config["allowedModules"].append(module["moduleName"])
            allowed_module_names.add(module["moduleName"].casefold())
    historical_catalog_fallback = config.get("historicalCatalogFallback")
    if historical_catalog_fallback not in (None, "current"):
        raise VersionedDocCError(
            "historicalCatalogFallback must be current when configured"
        )
    config.setdefault("symbolGraph", {})
    symbol_graph = config["symbolGraph"]
    if not isinstance(symbol_graph, dict):
        raise VersionedDocCError("symbolGraph must be an object")
    symbol_graph.setdefault("minimumAccessLevel", "public")
    symbol_graph.setdefault("skipProtocolImplementations", True)
    if "emitExtensionBlocks" in symbol_graph and not isinstance(
        symbol_graph["emitExtensionBlocks"], bool
    ):
        raise VersionedDocCError(
            "symbolGraph.emitExtensionBlocks must be a boolean"
        )
    platforms = symbol_graph.get("platforms")
    if platforms is not None:
        if not isinstance(platforms, list) or not platforms:
            raise VersionedDocCError("symbolGraph.platforms must be a non-empty array")
        names = set()
        for platform in platforms:
            if not isinstance(platform, dict):
                raise VersionedDocCError("each symbolGraph platform must be an object")
            name = platform.get("name")
            triple = platform.get("triple")
            if not isinstance(name, str) or not name.strip():
                raise VersionedDocCError("each symbolGraph platform requires a name")
            if not isinstance(triple, str) or not triple.strip():
                raise VersionedDocCError(
                    f"symbolGraph platform {name} requires a target triple"
                )
            normalized_name = name.casefold()
            if normalized_name in names:
                raise VersionedDocCError(f"duplicate symbolGraph platform: {name}")
            names.add(normalized_name)
            platform.setdefault("buildArguments", [])
            if not isinstance(platform["buildArguments"], list) or not all(
                isinstance(argument, str) for argument in platform["buildArguments"]
            ):
                raise VersionedDocCError(
                    f"symbolGraph platform {name} buildArguments must be strings"
                )
            for key in ("sdk", "swiftSDK"):
                value = platform.get(key)
                if value is not None and (not isinstance(value, str) or not value):
                    raise VersionedDocCError(
                        f"symbolGraph platform {name} {key} must be a non-empty string"
                    )
        default_platform = symbol_graph.setdefault("defaultPlatform", platforms[0]["name"])
        if not isinstance(default_platform, str) or default_platform.casefold() not in names:
            raise VersionedDocCError(
                "symbolGraph.defaultPlatform must match a configured platform name"
            )
        symbol_graph["platforms"] = sorted(
            platforms,
            key=lambda platform: platform["name"].casefold()
            != default_platform.casefold(),
        )
    elif "defaultPlatform" in symbol_graph:
        raise VersionedDocCError(
            "symbolGraph.defaultPlatform requires symbolGraph.platforms"
        )
    config.setdefault("apiChanges", {})
    if not isinstance(config["apiChanges"], dict):
        raise VersionedDocCError("apiChanges must be an object")
    config["apiChanges"].setdefault("pageSize", 10)
    page_size = config["apiChanges"]["pageSize"]
    if isinstance(page_size, bool) or not isinstance(page_size, int) or page_size < 1:
        raise VersionedDocCError("apiChanges.pageSize must be a positive integer")
    config.setdefault("articleChanges", {})
    if not isinstance(config["articleChanges"], dict):
        raise VersionedDocCError("articleChanges must be an object")
    config["articleChanges"].setdefault("enabled", False)
    if not isinstance(config["articleChanges"]["enabled"], bool):
        raise VersionedDocCError("articleChanges.enabled must be a boolean")
    config.setdefault("siteUI", {})
    site_ui = config["siteUI"]
    if not isinstance(site_ui, dict):
        raise VersionedDocCError("siteUI must be an object")
    repository_ui_default = bool(config.get("sourceRepository"))
    site_ui.setdefault("showEdit", repository_ui_default)
    site_ui.setdefault("showStar", repository_ui_default)
    site_ui.setdefault("showPoweredBy", True)
    for key in ("showEdit", "showStar", "showPoweredBy"):
        if not isinstance(site_ui[key], bool):
            raise VersionedDocCError(f"siteUI.{key} must be a boolean")
    if (site_ui["showEdit"] or site_ui["showStar"]) and not config.get(
        "sourceRepository"
    ):
        raise VersionedDocCError(
            "siteUI.showEdit and siteUI.showStar require sourceRepository"
        )
    if "ociCache" in config:
        oci_cache = config["ociCache"]
        if not isinstance(oci_cache, dict) or not oci_cache.get("repository"):
            raise VersionedDocCError("ociCache.repository is required")
        repository = oci_cache["repository"].rstrip("/")
        if repository.startswith("oci-layout://"):
            if not repository.removeprefix("oci-layout://").startswith("/"):
                raise VersionedDocCError("oci-layout repository must use an absolute path")
        elif not re.fullmatch(r"[A-Za-z0-9._:-]+(?:/[A-Za-z0-9._-]+)+", repository):
            raise VersionedDocCError(f"invalid OCI repository: {repository}")
        oci_cache["repository"] = repository
        oci_cache.setdefault("pull", True)
        oci_cache.setdefault("includeDevelopment", False)
    return config, path


def modules_for_version(config, version):
    modules = [
        {
            "moduleName": config["moduleName"],
            "modulePath": config["modulePath"],
            "targetName": config["targetName"],
            "catalogPath": version.get("catalogPath", config["catalogPath"]),
            "primary": True,
        }
    ]
    for module in config["additionalModules"]:
        modules.append({**module, "primary": False})
    return modules


def configured_module_names(config, version=None):
    if config["documentationOnly"]:
        return []
    if version is not None:
        return [module["moduleName"] for module in modules_for_version(config, version)]
    return [
        config["moduleName"],
        *[module["moduleName"] for module in config["additionalModules"]],
    ]


def configured_module_paths(config, version=None):
    if config["documentationOnly"]:
        return [config["modulePath"]]
    if version is not None:
        return [module["modulePath"] for module in modules_for_version(config, version)]
    return [
        config["modulePath"],
        *[module["modulePath"] for module in config["additionalModules"]],
    ]


def platform_slug(value):
    slug = re.sub(r"[^A-Za-z0-9._-]", "-", value.strip()).strip("-._")
    return slug.lower() or "platform"


def configured_symbol_graph_platforms(config):
    return config["symbolGraph"].get("platforms", [])


def resolve_platform_sdk(platform):
    sdk = platform.get("sdk")
    if not sdk:
        return None
    path = Path(sdk).expanduser()
    if path.is_absolute():
        if not path.is_dir():
            raise VersionedDocCError(
                f"missing SDK for symbolGraph platform {platform['name']}: {path}"
            )
        return str(path)
    if not shutil.which("xcrun"):
        raise VersionedDocCError(
            f"symbolGraph platform {platform['name']} SDK {sdk} requires xcrun"
        )
    return run(["xcrun", "--sdk", sdk, "--show-sdk-path"], capture=True)


def module_symbol_graph_paths(symbols_root, module_name):
    paths = set(symbols_root.rglob(f"{module_name}.symbols.json"))
    paths.update(symbols_root.rglob(f"{module_name}@*.symbols.json"))
    return sorted(paths)


def parsed_release_tag(tag, timestamp=0):
    match = SEMVER.fullmatch(tag)
    if not match:
        return None
    version = tuple(map(int, match.groups()))
    normalized_tag = tag.removeprefix("v")
    tag_without_build_metadata = normalized_tag.split("+", 1)[0]
    is_stable = "-" not in tag_without_build_metadata
    return timestamp, version, is_stable, normalized_tag, tag


def release_versions(repository, count, strategy="majorMinor"):
    if strategy not in LATEST_RELEASE_STRATEGIES:
        raise VersionedDocCError(f"invalid latest release strategy: {strategy}")

    parsed = []
    if strategy == "tagDate":
        lines = git(
            repository,
            "for-each-ref",
            "--format=%(creatordate:unix)\t%(refname:strip=2)",
            "refs/tags",
        ).splitlines()
        for line in lines:
            timestamp_text, separator, tag = line.partition("\t")
            if not separator:
                continue
            try:
                timestamp = int(timestamp_text)
            except ValueError:
                continue
            entry = parsed_release_tag(tag, timestamp)
            if entry:
                parsed.append(entry)
        parsed.sort(reverse=True)
    else:
        for tag in git(repository, "tag", "--list").splitlines():
            entry = parsed_release_tag(tag)
            if entry:
                parsed.append(entry)
        parsed.sort(key=lambda entry: entry[1:], reverse=True)

    selected = []
    selected_keys = set()
    for _, version, _, normalized_tag, tag in parsed:
        key = version[:2] if strategy == "majorMinor" else normalized_tag
        if key in selected_keys:
            continue
        selected.append(tag)
        selected_keys.add(key)
        if len(selected) == count:
            break
    return selected


def semantic_versions(repository, count):
    return release_versions(repository, count, "majorMinor")


def semantic_version_series(tag):
    match = SEMVER.fullmatch(tag)
    if not match:
        return None
    return tuple(map(int, match.groups()[:2]))


def source_reference(config, version):
    reference = version.get("sourceRef", version["ref"])
    if reference == "HEAD":
        return config.get("developmentSourceRef", "main")
    return reference


def configured_versions(repository, config):
    if config.get("versions"):
        versions = config["versions"]
    else:
        policy = config.get("releasePolicy", {"latest": 2})
        if not isinstance(policy, dict):
            raise VersionedDocCError("releasePolicy must be an object")
        development = policy.get("development", {"name": "main", "ref": "HEAD"})
        versions = [development]
        latest = policy.get("latest", 2)
        if isinstance(latest, bool) or not isinstance(latest, int) or latest < 1:
            raise VersionedDocCError("releasePolicy.latest must be a positive integer")
        latest_strategy = policy.get("latestStrategy", "majorMinor")
        selected_tags = release_versions(repository, latest, latest_strategy)
        if latest_strategy == "majorMinor":
            selected_keys = {semantic_version_series(tag) for tag in selected_tags}
        else:
            selected_keys = {tag.removeprefix("v") for tag in selected_tags}
        for tag in policy.get("pinned", []):
            if not isinstance(tag, str) or not SEMVER.fullmatch(tag):
                raise VersionedDocCError(f"invalid pinned release: {tag}")
            if latest_strategy == "majorMinor":
                key = semantic_version_series(tag)
            else:
                key = tag.removeprefix("v")
            if key not in selected_keys:
                selected_tags.append(tag)
                selected_keys.add(key)
        versions.extend(
            {"name": tag.lstrip("v"), "ref": tag}
            for tag in selected_tags
        )
    names = set()
    normalized = []
    for item in versions:
        name = item.get("name")
        ref = item.get("ref", "HEAD" if name == "main" else name)
        if not name or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
            raise VersionedDocCError(f"invalid version name: {name}")
        if name in names:
            raise VersionedDocCError(f"duplicate version name: {name}")
        names.add(name)
        normalized_version = {"name": name, "ref": ref}
        source_ref = item.get("sourceRef")
        if source_ref is not None:
            if not isinstance(source_ref, str) or not source_ref:
                raise VersionedDocCError(
                    f"sourceRef for {name} must be a non-empty string"
                )
            normalized_version["sourceRef"] = source_ref
        catalog_path = item.get("catalogPath")
        if catalog_path is not None:
            if not isinstance(catalog_path, str) or not catalog_path:
                raise VersionedDocCError(
                    f"catalogPath for {name} must be a non-empty string"
                )
            normalized_version["catalogPath"] = catalog_path
        normalized.append(normalized_version)
    if config["defaultVersion"] not in names:
        raise VersionedDocCError(
            f"defaultVersion {config['defaultVersion']} isn't in the version list"
        )
    if len(normalized) < 2:
        raise VersionedDocCError("at least two documentation versions are required")
    return normalized


def resolved_versions(repository, config):
    return [
        {
            **version,
            "commit": git(repository, "rev-parse", f"{version['ref']}^{{commit}}"),
        }
        for version in configured_versions(repository, config)
    ]


def find_docc():
    if shutil.which("xcrun"):
        return ["xcrun", "docc"], Path(run(["xcrun", "--find", "docc"], capture=True))
    executable = shutil.which("docc")
    if executable:
        return [executable], Path(executable)
    raise VersionedDocCError("DocC is not installed")


def swift_module(precise_identifier):
    if not precise_identifier.startswith("s:"):
        return None
    remainder = precise_identifier[2:]
    match = re.match(r"(\d+)", remainder)
    if not match:
        return None
    length = int(match.group(1))
    start = len(match.group(1))
    return remainder[start : start + length]


class ClangUSR:
    MODULE = re.compile(r"^c:@M@([^@]+)(?:@|$)")

    def __init__(self, precise_identifier):
        self.precise_identifier = precise_identifier

    @classmethod
    def parse(cls, precise_identifier):
        if not precise_identifier.startswith("c:"):
            return None
        return cls(precise_identifier)

    @property
    def module(self):
        match = self.MODULE.match(self.precise_identifier)
        return match.group(1) if match else None


def symbol_module(precise_identifier):
    module = swift_module(precise_identifier)
    if module is not None:
        return module
    clang_usr = ClangUSR.parse(precise_identifier)
    return clang_usr.module if clang_usr is not None else None


def filter_symbol_graph(path, allowed_modules):
    with path.open(encoding="utf-8") as source:
        graph = json.load(source)
    original_symbols = graph.get("symbols", [])
    symbols = [
        symbol
        for symbol in original_symbols
        if (
            symbol_module(symbol.get("identifier", {}).get("precise", ""))
            in allowed_modules
        )
    ]
    identifiers = {symbol["identifier"]["precise"] for symbol in symbols}
    original_relationships = graph.get("relationships", [])
    relationships = [
        relationship
        for relationship in original_relationships
        if relationship.get("source") in identifiers
        and (
            relationship.get("target") in identifiers
            or relationship.get("targetFallback") is not None
        )
    ]
    graph["symbols"] = symbols
    graph["relationships"] = relationships
    with path.open("w", encoding="utf-8") as destination:
        json.dump(graph, destination, separators=(",", ":"))
        destination.write("\n")
    print(
        f"{path.name}: {len(original_symbols)} -> {len(symbols)} symbols; "
        f"{len(original_relationships)} -> {len(relationships)} relationships"
    )


def retain_symbol_graph_module(graph_directory, module_name):
    # SwiftPM emits dependency graphs into the same directory. Use the graph's
    # module metadata instead of its filename so extension graphs such as
    # Module@Foundation.symbols.json remain part of the requested module.
    retained = []
    removed = []
    for path in sorted(graph_directory.glob("*.symbols.json")):
        with path.open(encoding="utf-8") as source:
            graph = json.load(source)
        if graph.get("module", {}).get("name") == module_name:
            retained.append(path)
        else:
            path.unlink()
            removed.append(path)
    if not retained:
        raise VersionedDocCError(
            f"no symbol graphs for module {module_name} under {graph_directory}"
        )
    print(
        f"{module_name}: retained {len(retained)} module symbol graph(s); "
        f"removed {len(removed)} dependency symbol graph(s)"
    )
    return retained


class PreparedSource:
    def __init__(self, package_root, config, version, commit):
        self.package_root = package_root
        self.config = config
        self.version = version
        self.commit = commit
        self.root = None
        self.temporary_root = None

    @staticmethod
    def clone_at_revision(repository, destination, revision):
        # A command plugin may read the package's Git repository, but it can't
        # update the canonical repository's .git/worktrees directory. A shared
        # clone keeps all mutable Git state in the temporary directory while
        # reusing the source repository's object database read-only.
        run(
            [
                "git",
                "clone",
                "--quiet",
                "--shared",
                "--no-checkout",
                str(repository),
                str(destination),
            ]
        )
        run(["git", "-C", str(destination), "checkout", "--quiet", "--detach", revision])

    def __enter__(self):
        dependencies = self.config.get("localDependencies", {})
        current_commit = git(self.package_root, "rev-parse", "HEAD")
        if not dependencies and self.commit == current_commit:
            self.root = self.package_root
            return self.root

        self.temporary_root = Path(
            tempfile.mkdtemp(prefix=f"versioned-docc-{self.version['name']}-")
        )
        self.root = self.temporary_root / self.package_root.name
        self.clone_at_revision(self.package_root, self.root, self.commit)
        if not dependencies:
            return self.root

        resolved_path = self.root / "Package.resolved"
        if not resolved_path.is_file():
            raise VersionedDocCError(f"{self.version['name']} has no Package.resolved")
        with resolved_path.open(encoding="utf-8") as source:
            resolved = json.load(source)
        pins = {pin["identity"].lower(): pin for pin in resolved.get("pins", [])}
        for identity, configured_path in dependencies.items():
            pin = pins.get(identity.lower())
            revision = pin and pin.get("state", {}).get("revision")
            if not revision:
                raise VersionedDocCError(
                    f"{self.version['name']} doesn't pin {identity} in Package.resolved"
                )
            repository = resolve_path(self.package_root, configured_path)
            if not (repository / ".git").exists() and not (repository / ".git").is_file():
                raise VersionedDocCError(f"missing local dependency repository: {repository}")
            try:
                git(repository, "cat-file", "-e", f"{revision}^{{commit}}")
            except VersionedDocCError:
                print(f"Fetching {identity} revision {revision}")
                run(["git", "-C", str(repository), "fetch", "origin", revision])
            destination = self.temporary_root / repository.name
            self.clone_at_revision(repository, destination, revision)
        return self.root

    def __exit__(self, exc_type, exc_value, traceback):
        if self.temporary_root and self.temporary_root.exists():
            shutil.rmtree(self.temporary_root, ignore_errors=True)


def article_changes_enabled(config):
    return config.get("articleChanges", {}).get("enabled", False)


def changes_enabled(config):
    return not config["documentationOnly"] or article_changes_enabled(config)


def external_link_attributes():
    return 'target="_blank" rel="noopener noreferrer"'


def site_ui_value(config, key):
    default = True if key == "showPoweredBy" else bool(config.get("sourceRepository"))
    return config.get("siteUI", {}).get(key, default)


def star_link(config):
    if not site_ui_value(config, "showStar"):
        return ""
    repository = html.escape(config["sourceRepository"].rstrip("/"), quote=True)
    return (
        f'<a class="versioned-docc-star" href="{repository}" '
        f'{external_link_attributes()} aria-label="Star {html.escape(config["projectName"])} on GitHub">'
        '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 0C3.58 0 0 3.64 0 8.13c0 3.59 '
        '2.29 6.64 5.47 7.71.4.08.55-.18.55-.39 0-.19-.01-.83-.01-1.51-2.01.38-2.53-.5-2.69-.96-.09-.23-.48-.96-.82-1.15-.28-.15-.68-.52-.01-.53.63-.01 1.08.59 1.23.83.72 1.23 1.87.88 2.33.67.07-.53.28-.88.51-1.08-1.78-.21-3.64-.91-3.64-4.02 0-.89.31-1.62.82-2.19-.08-.2-.36-1.04.08-2.16 0 0 .67-.22 2.2.84A7.5 7.5 0 0 1 8 3.94a7.5 7.5 0 0 1 2 .27c1.53-1.06 2.2-.84 2.2-.84.44 1.12.16 1.96.08 2.16.51.57.82 1.3.82 2.19 0 3.12-1.87 3.81-3.65 4.02.29.25.54.74.54 1.5 0 1.08-.01 1.95-.01 2.22 0 .22.15.48.55.39A8.14 8.14 0 0 0 16 8.13C16 3.64 12.42 0 8 0Z"/></svg>'
        '<span>Star on GitHub</span></a>'
    )


def render_header(template, config, version, build_date):
    base = config["hostingBasePath"]
    module_path = config["modulePath"]
    replacements = {
        "__VERSIONED_DOCC_PROJECT_NAME__": html.escape(config["projectName"]),
        "__VERSIONED_DOCC_HOME_PATH__": f"{base}/{version}/documentation/{module_path}/",
        "__VERSIONED_DOCC_CHANGES_LINK__": (
            f'<a class="versioned-docc-changes" '
            f'href="{base}/{config["defaultVersion"]}/changes/">'
            f'{"Changes" if article_changes_enabled(config) else "API Changes"}</a>'
        )
        if changes_enabled(config)
        else "",
        "__VERSIONED_DOCC_BUILD_DATE__": build_date,
        "__VERSIONED_DOCC_CURRENT_VERSION__": version,
        "__VERSIONED_DOCC_STAR_LINK__": star_link(config),
    }
    for token, value in replacements.items():
        template = template.replace(token, value)
    return template


def render_footer(template, config, version):
    show_edit = site_ui_value(config, "showEdit")
    show_powered_by = site_ui_value(config, "showPoweredBy")
    if not show_edit and not show_powered_by:
        return ""
    edit_link = (
        f'<a class="versioned-docc-edit" href="#" hidden '
        f'{external_link_attributes()}>Edit this page</a>'
        if show_edit
        else ""
    )
    powered_by_link = (
        f'<a class="versioned-docc-powered-by" '
        f'href="{VERSIONED_DOCC_REPOSITORY}" {external_link_attributes()}>'
        "Powered by VersionedDocC</a>"
        if show_powered_by
        else ""
    )
    replacements = {
        "__VERSIONED_DOCC_EDIT_LINK__": edit_link,
        "__VERSIONED_DOCC_POWERED_BY_LINK__": powered_by_link,
        "__VERSIONED_DOCC_SITE_ROOT_JSON__": json.dumps(
            f'{config["hostingBasePath"]}/{version}'
        ),
    }
    for token, value in replacements.items():
        template = template.replace(token, value)
    return template


def append_custom_footer(catalog, rendered_footer):
    if not rendered_footer:
        return
    footer_path = catalog / "footer.html"
    existing_footer = (
        footer_path.read_text(encoding="utf-8") if footer_path.is_file() else ""
    )
    separator = ""
    if existing_footer:
        separator = "\n" if existing_footer.endswith("\n") else "\n\n"
    footer_path.write_text(
        existing_footer + separator + rendered_footer,
        encoding="utf-8",
    )


def prepared_dependency_roots(package_root, config, source_root):
    roots = {}
    for identity, configured_path in config.get("localDependencies", {}).items():
        repository = resolve_path(package_root, configured_path)
        roots[identity.casefold()] = (source_root.parent / repository.name).resolve()
    return roots


def module_source_route(package_root, config, version, source_root, module=None):
    module = module or {}
    source_repository = module.get(
        "sourceRepository", config.get("sourceRepository")
    )
    if not source_repository:
        return None

    configured_root = module.get("sourceRoot")
    if configured_root is None:
        checkout_root = source_root.resolve()
        reference = source_reference(config, version)
    else:
        checkout_root = resolve_path(source_root, configured_root).resolve()
        dependency_roots = prepared_dependency_roots(
            package_root, config, source_root
        )
        matching_dependencies = [
            identity
            for identity, dependency_root in dependency_roots.items()
            if dependency_root == checkout_root
        ]
        if not matching_dependencies:
            raise VersionedDocCError(
                f"sourceRoot for {module['moduleName']} must identify a prepared "
                "localDependencies checkout"
            )
        if not checkout_root.is_dir():
            raise VersionedDocCError(
                f"missing sourceRoot for {module['moduleName']}: {checkout_root}"
            )
        repository_root = Path(
            git(checkout_root, "rev-parse", "--show-toplevel")
        ).resolve()
        if repository_root != checkout_root:
            raise VersionedDocCError(
                f"sourceRoot for {module['moduleName']} is not a Git checkout root: "
                f"{checkout_root}"
            )
        reference = git(checkout_root, "rev-parse", "HEAD")

    return {
        "repository": source_repository.rstrip("/"),
        "reference": reference,
        "checkoutRoot": checkout_root,
    }


def source_service_arguments(
    config, version, source_root, module=None, package_root=None
):
    route = module_source_route(
        package_root or source_root,
        config,
        version,
        source_root,
        module,
    )
    if route is None:
        return []
    return [
        "--source-service",
        "github",
        "--source-service-base-url",
        f"{route['repository']}/blob/{route['reference']}",
        "--checkout-path",
        str(route["checkoutRoot"]),
    ]


def github_edit_url(source_url):
    if not isinstance(source_url, str):
        return None
    parsed = urlsplit(source_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    marker = "/blob/"
    if marker not in parsed.path:
        return None
    return source_url.replace(marker, "/edit/", 1)


def repository_edit_url(repository, reference, source_path):
    encoded_reference = quote(reference, safe="/@:+-._~")
    encoded_path = quote(Path(source_path).as_posix(), safe="/@:+-._~()")
    return f"{repository.rstrip('/')}/edit/{encoded_reference}/{encoded_path}"


def edit_source_key(value):
    value = unquote(value).strip().strip("`")
    return re.sub(r"\s+", "-", value).casefold()


def markdown_edit_sources(catalog, repository_catalog_path, repository, reference):
    candidates = {}
    technology_roots = []

    def register(key, candidate):
        key = edit_source_key(key)
        if not key:
            return
        existing = candidates.get(key)
        candidates[key] = candidate if existing in (None, candidate) else False

    for markdown in sorted(catalog.rglob("*.md")):
        relative_path = markdown.relative_to(catalog)
        repository_path = Path(repository_catalog_path) / relative_path
        candidate = {
            "editURL": repository_edit_url(repository, reference, repository_path),
            "fileName": markdown.name,
        }
        register(markdown.stem, candidate)
        contents = markdown.read_text(encoding="utf-8", errors="replace")
        if "@TechnologyRoot" in contents:
            technology_roots.append(candidate)
        heading = re.search(r"(?m)^#\s+``([^`]+)``\s*$", contents)
        if heading:
            symbol_name = heading.group(1).rsplit("/", 1)[-1]
            register(symbol_name, candidate)
    return candidates, technology_roots


def inject_edit_metadata(
    site_root,
    catalog,
    repository_catalog_path,
    config,
    version,
    edit_reference,
    authored_archive_identifier=None,
    source_repository=None,
):
    if not site_ui_value(config, "showEdit"):
        return 0
    repository = (source_repository or config["sourceRepository"]).rstrip("/")
    candidates, technology_roots = markdown_edit_sources(
        catalog,
        repository_catalog_path,
        repository,
        edit_reference,
    )
    documentation_root = site_root / "data" / "documentation"
    injected = 0
    authored = 0
    remote = 0
    if not documentation_root.is_dir():
        return 0
    for path in documentation_root.rglob("*.json"):
        with path.open(encoding="utf-8") as source:
            document = json.load(source)
        metadata = document.get("metadata", {})
        identifier = document.get("identifier", {}).get("url", "")
        identifier_component = identifier.rsplit("/", 1)[-1]
        candidate = candidates.get(edit_source_key(identifier_component))
        if candidate is False:
            candidate = None
        if (
            candidate is not None
            and authored_archive_identifier is not None
            and urlsplit(identifier).netloc.casefold()
            != authored_archive_identifier.casefold()
        ):
            candidate = None
        if (
            candidate is None
            and config["documentationOnly"]
            and metadata.get("role") == "collection"
            and len(technology_roots) == 1
        ):
            candidate = technology_roots[0]
        edit_url = candidate.get("editURL") if candidate else None
        source_file = candidate.get("fileName") if candidate else None
        if edit_url:
            authored += 1
        else:
            edit_url = github_edit_url(metadata.get("remoteSource", {}).get("url"))
            if edit_url:
                remote += 1
        if not edit_url:
            continue
        versioned_metadata = metadata.setdefault("versionedDocC", {})
        versioned_metadata["editURL"] = edit_url
        if source_file:
            versioned_metadata["sourceFile"] = source_file
        document["metadata"] = metadata
        path.write_text(
            json.dumps(document, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        injected += 1
    print(
        f"{version['name']}: added edit links to {injected} pages "
        f"({authored} authored, {remote} source declarations)"
    )
    return injected


def build_fingerprint(config, docc_binary, header_template, footer_template=""):
    renderer_path = os.environ.get("DOCC_HTML_DIR")
    if renderer_path:
        renderer = resolve_path(Path.cwd(), renderer_path)
        try:
            renderer_id = git(renderer, "rev-parse", "HEAD")
        except VersionedDocCError:
            renderer_id = sha256_file(renderer / "index.html")
    else:
        renderer_id = "bundled"
    payload = {
        "versionedDocC": BUILD_CACHE_REVISION,
        "swift": run(["swift", "--version"], capture=True),
        "docc": sha256_file(docc_binary),
        "renderer": renderer_id,
        "header": sha256_bytes(header_template.encode()),
        "footer": sha256_bytes(footer_template.encode()),
        "hostingBasePath": config.get("hostingBasePath", ""),
        "siteUI": config.get("siteUI", {}),
        "documentationOnly": config.get("documentationOnly", False),
        "target": config.get("targetName"),
        "module": config.get("moduleName"),
        "catalog": config["catalogPath"],
        "environment": config["environment"],
        "buildArguments": config["buildArguments"],
        "doccArguments": config["doccArguments"],
        "symbolGraph": config["symbolGraph"],
        "allowedModules": config["allowedModules"],
        "additionalModules": config.get("additionalModules", []),
        "historicalCatalogFallback": config.get("historicalCatalogFallback"),
    }
    if article_changes_enabled(config):
        payload["articleChanges"] = True
    return sha256_bytes(json.dumps(payload, sort_keys=True).encode())


def version_cache_fingerprint(build_fingerprint, config, version):
    source_repository = config.get("sourceRepository")
    source_routing = None
    if source_repository:
        source_routing = {
            "repository": source_repository.rstrip("/"),
            "ref": source_reference(config, version),
        }
    payload = {
        "buildFingerprint": build_fingerprint,
        "sourceRouting": source_routing,
        "catalogPath": version.get("catalogPath", config.get("catalogPath")),
    }
    return sha256_bytes(json.dumps(payload, sort_keys=True).encode())


def cache_valid(cache_entry, commit, fingerprint, module_name=None):
    metadata_path = cache_entry / "metadata.json"
    site_path = cache_entry / "site" / "index.html"
    module_names = (
        [module_name]
        if isinstance(module_name, str)
        else list(module_name or [])
    )
    missing_graphs = [
        name
        for name in module_names
        if not module_symbol_graph_paths(cache_entry / "symbols", name)
    ]
    if (
        not metadata_path.is_file()
        or missing_graphs
        or not site_path.is_file()
    ):
        return False
    with metadata_path.open(encoding="utf-8") as source:
        metadata = json.load(source)
    return metadata.get("sourceCommit") == commit and metadata.get("buildFingerprint") == fingerprint


def uses_oci_cache(config, version):
    oci_cache = config.get("ociCache")
    if not oci_cache:
        return False
    return oci_cache.get("includeDevelopment", False) or bool(
        SEMVER.fullmatch(version["name"])
    )


def oci_cache_tag(version, commit, fingerprint):
    identity = sha256_bytes(f"{version['name']}\0{commit}\0{fingerprint}".encode())[:32]
    version_name = re.sub(r"[^A-Za-z0-9._-]", "-", version["name"])
    return f"cache-{version_name[:80]}-{identity}"


def oci_target(repository, tag):
    if repository.startswith("oci-layout://"):
        return ["--oci-layout"], f"{repository.removeprefix('oci-layout://')}:{tag}"
    return [], f"{repository}:{tag}"


def oci_reference(config, version, commit, fingerprint):
    tag = oci_cache_tag(version, commit, fingerprint)
    return f"{config['ociCache']['repository']}:{tag}"


def find_oras():
    configured = os.environ.get("VERSIONED_DOCC_ORAS")
    if configured:
        executable = shutil.which(configured) or configured
        path = Path(executable).expanduser()
        if path.is_file():
            return str(path.resolve())
        raise VersionedDocCError(f"configured ORAS executable not found: {configured}")
    return shutil.which("oras")


def oci_artifact_exists(oras, repository, tag):
    target_options, target = oci_target(repository, tag)
    result = run_status(
        [oras, "manifest", "fetch", *target_options, target]
    )
    if result.returncode == 0:
        try:
            manifest = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise VersionedDocCError(f"invalid OCI manifest: {target}") from error
        if manifest.get("artifactType") != OCI_ARTIFACT_TYPE:
            raise VersionedDocCError(
                f"unexpected OCI artifact type for {target}: "
                f"{manifest.get('artifactType')}"
            )
        return True
    details = f"{result.stdout}\n{result.stderr}".lower()
    missing_markers = (
        "not found",
        "no such file or directory",
        "manifest unknown",
        "name unknown",
        "404",
    )
    if any(marker in details for marker in missing_markers):
        return False
    raise VersionedDocCError(
        f"unable to query OCI cache {target}:\n{(result.stderr or result.stdout).strip()}"
    )


def validate_oci_metadata(oras, repository, tag, version, commit, fingerprint):
    target_options, target = oci_target(repository, tag)
    contents = run(
        [oras, "manifest", "fetch-config", *target_options, target],
        capture=True,
    )
    try:
        metadata = json.loads(contents)
    except json.JSONDecodeError as error:
        raise VersionedDocCError(f"invalid OCI cache metadata: {target}") from error
    expected = {
        "version": version["name"],
        "sourceCommit": commit,
        "buildFingerprint": fingerprint,
    }
    mismatches = [
        key for key, value in expected.items() if metadata.get(key) != value
    ]
    if mismatches:
        raise VersionedDocCError(
            f"OCI cache metadata mismatch for {target}: {', '.join(mismatches)}"
        )
    return metadata


def create_cache_archive(cache_entry, archive_path):
    with archive_path.open("wb") as raw_archive:
        with gzip.GzipFile(
            filename="",
            mode="wb",
            fileobj=raw_archive,
            compresslevel=6,
            mtime=0,
        ) as compressed:
            with tarfile.open(fileobj=compressed, mode="w|") as archive:
                for path in sorted(cache_entry.rglob("*")):
                    if path.is_symlink():
                        raise VersionedDocCError(f"OCI cache cannot archive symlink: {path}")
                    relative = path.relative_to(cache_entry).as_posix()
                    information = archive.gettarinfo(str(path), arcname=relative)
                    information.uid = 0
                    information.gid = 0
                    information.uname = ""
                    information.gname = ""
                    information.mtime = 0
                    if path.is_file():
                        with path.open("rb") as source:
                            archive.addfile(information, source)
                    else:
                        archive.addfile(information)


def extract_cache_archive(archive_path, destination):
    destination.mkdir(parents=True)
    with tarfile.open(archive_path, mode="r:gz") as archive:
        for member in archive:
            if not (member.isfile() or member.isdir()):
                raise VersionedDocCError(f"unsupported OCI cache entry: {member.name}")
            member_path = Path(member.name)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise VersionedDocCError(f"unsafe OCI cache entry: {member.name}")
            output = destination / member_path
            if member.isdir():
                output.mkdir(parents=True, exist_ok=True)
                continue
            output.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise VersionedDocCError(f"unable to extract OCI cache entry: {member.name}")
            with source, output.open("wb") as target:
                shutil.copyfileobj(source, target)
            output.chmod(member.mode & 0o777)


def restore_oci_cache(
    oras,
    config,
    version,
    commit,
    fingerprint,
    cache_root,
):
    repository = config["ociCache"]["repository"]
    tag = oci_cache_tag(version, commit, fingerprint)
    reference = oci_reference(config, version, commit, fingerprint)
    if not oci_artifact_exists(oras, repository, tag):
        print(f"OCI cache miss: {version['name']} ({reference})")
        return False
    validate_oci_metadata(oras, repository, tag, version, commit, fingerprint)

    temporary = Path(tempfile.mkdtemp(prefix=".oci-pull-", dir=cache_root))
    staging = cache_root / f".restoring-{version['name']}-{os.getpid()}"
    remove_tree(staging, cache_root, "OCI cache staging directory")
    try:
        download = temporary / "download"
        download.mkdir()
        target_options, target = oci_target(repository, tag)
        run([oras, "pull", "--output", str(download), *target_options, target])
        archive_path = download / OCI_ARCHIVE_NAME
        if not archive_path.is_file():
            raise VersionedDocCError(f"OCI cache has no {OCI_ARCHIVE_NAME}: {reference}")
        extract_cache_archive(archive_path, staging)
        if not cache_valid(
            staging,
            commit,
            fingerprint,
            configured_module_names(config, version),
        ):
            raise VersionedDocCError(f"OCI cache metadata mismatch: {reference}")
        cache_entry = cache_root / version["name"]
        remove_tree(cache_entry, cache_root, "version cache")
        staging.rename(cache_entry)
    except Exception:
        remove_tree(staging, cache_root, "failed OCI cache staging directory")
        raise
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
    print(f"OCI cache restored: {version['name']} ({reference})")
    return True


def publish_oci_cache(
    oras,
    config,
    version,
    commit,
    fingerprint,
    cache_root,
):
    repository = config["ociCache"]["repository"]
    tag = oci_cache_tag(version, commit, fingerprint)
    reference = oci_reference(config, version, commit, fingerprint)
    if oci_artifact_exists(oras, repository, tag):
        validate_oci_metadata(oras, repository, tag, version, commit, fingerprint)
        print(f"OCI cache exists: {version['name']} ({reference})")
        return False

    cache_entry = cache_root / version["name"]
    temporary = Path(tempfile.mkdtemp(prefix=".oci-push-", dir=cache_root))
    try:
        archive_path = temporary / OCI_ARCHIVE_NAME
        create_cache_archive(cache_entry, archive_path)
        metadata_name = "metadata.json"
        shutil.copy2(cache_entry / metadata_name, temporary / metadata_name)
        target_options, target = oci_target(repository, tag)
        command = [
            oras,
            "push",
            "--artifact-type",
            OCI_ARTIFACT_TYPE,
            "--config",
            f"{metadata_name}:{OCI_CONFIG_TYPE}",
            "--annotation",
            f"org.opencontainers.image.version={version['name']}",
            "--annotation",
            f"org.opencontainers.image.revision={commit}",
        ]
        source_repository = config.get("sourceRepository")
        if source_repository:
            command.extend(
                ["--annotation", f"org.opencontainers.image.source={source_repository}"]
            )
        command.extend(
            [*target_options, target, f"{OCI_ARCHIVE_NAME}:{OCI_LAYER_TYPE}"]
        )
        run(command, cwd=temporary)
        digest = run([oras, "resolve", *target_options, target], capture=True)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
    print(f"OCI cache published: {version['name']} ({digest})")
    return True


def build_symbol_graphs(source_root, config, version, graph_root, logs_root):
    platforms = configured_symbol_graph_platforms(config)
    graph_builds = (
        [
            (
                platform,
                graph_root / f"{index:02d}-{platform_slug(platform['name'])}",
            )
            for index, platform in enumerate(platforms)
        ]
        if platforms
        else [(None, graph_root)]
    )
    for platform, graph_directory in graph_builds:
        graph_directory.mkdir(parents=True, exist_ok=True)
        platform_arguments = []
        log_suffix = ""
        if platform is not None:
            print(f"Building {version['name']} [{platform['name']}]")
            log_suffix = f"-{platform_slug(platform['name'])}"
            platform_arguments.extend(["--triple", platform["triple"]])
            sdk = resolve_platform_sdk(platform)
            if sdk:
                platform_arguments.extend(["--sdk", sdk])
            swift_sdk = platform.get("swiftSDK")
            if swift_sdk:
                platform_arguments.extend(["--swift-sdk", swift_sdk])
            platform_arguments.extend(platform["buildArguments"])
        build_command = [
            "swift",
            "build",
            # Command plugins already run inside SwiftPM's sandbox. A nested
            # swift build can't apply a second sandbox profile.
            "--disable-sandbox",
            "--package-path",
            str(source_root),
            "--target",
            config["targetName"],
            *config["buildArguments"],
            *platform_arguments,
            "-Xswiftc",
            "-emit-symbol-graph",
            "-Xswiftc",
            "-emit-symbol-graph-dir",
            "-Xswiftc",
            str(graph_directory),
            "-Xswiftc",
            "-symbol-graph-minimum-access-level",
            "-Xswiftc",
            config["symbolGraph"]["minimumAccessLevel"],
        ]
        if config["symbolGraph"].get("emitExtensionBlocks", False):
            build_command.extend(["-Xswiftc", "-emit-extension-block-symbols"])
        if config["symbolGraph"].get("skipProtocolImplementations", True):
            # Force the symbol-graph option through SwiftPM's driver. Passing it
            # as a plain driver option doesn't reach the frontend emit-module job.
            build_command.extend(
                [
                    "-Xswiftc",
                    "-Xfrontend",
                    "-Xswiftc",
                    "-skip-protocol-implementations",
                ]
            )
        build_environment = dict(config["environment"])
        build_environment["VDC_GENERATE_DOCS"] = "1"
        run(
            build_command,
            cwd=source_root,
            environment=build_environment,
            log_path=logs_root
            / f"{version['name']}{log_suffix}-swift-build.log",
        )
        graph_path = graph_directory / f"{config['moduleName']}.symbols.json"
        if not graph_path.is_file():
            raise VersionedDocCError(f"missing symbol graph: {graph_path}")
        filter_symbol_graph(graph_path, set(config["allowedModules"]))
        retain_symbol_graph_module(graph_directory, config["moduleName"])
    return platforms


def external_symbol_graph_path(package_root, module, version, commit):
    values = {
        "version": version["name"],
        "ref": version["ref"],
        "commit": commit,
        "module": module["moduleName"],
    }
    try:
        configured = module["symbolGraphPath"].format_map(values)
    except KeyError as error:
        raise VersionedDocCError(
            f"unknown symbolGraphPath placeholder for {module['moduleName']}: {error.args[0]}"
        ) from error
    return resolve_path(package_root, configured)


def staged_catalog_path(
    catalog_root, catalog_name, module_name, index, module_count
):
    if module_count == 1:
        return catalog_root / catalog_name
    return (
        catalog_root
        / f"{index:02d}-{platform_slug(module_name)}"
        / catalog_name
    )


def rewrite_symbol_graph_locations(graph_path, source_root):
    with graph_path.open(encoding="utf-8") as source:
        graph = json.load(source)
    rewritten = 0
    for symbol in graph.get("symbols", []):
        location = symbol.get("location")
        if not isinstance(location, dict):
            continue
        uri = location.get("uri")
        if not isinstance(uri, str) or not uri.startswith("file://"):
            continue
        original = Path(unquote(urlsplit(uri).path))
        if original.is_file() and source_root.resolve() in original.resolve().parents:
            continue
        parts = original.parts
        replacement = None
        for marker in ("Sources", "Tests", "Plugins"):
            indexes = [index for index, part in enumerate(parts) if part == marker]
            for index in reversed(indexes):
                candidate = source_root.joinpath(*parts[index:])
                if candidate.is_file():
                    replacement = candidate.resolve().as_uri()
                    break
            if replacement:
                break
        if replacement:
            location["uri"] = replacement
            rewritten += 1
    if rewritten:
        graph_path.write_text(
            json.dumps(graph, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
    return rewritten


def import_symbol_graphs(
    package_root,
    source_root,
    module,
    version,
    commit,
    destination,
    allowed_modules,
):
    source_path = external_symbol_graph_path(package_root, module, version, commit)
    if source_path.is_file():
        candidates = [source_path]
    elif source_path.is_dir():
        candidates = sorted(source_path.rglob("*.symbols.json"))
    else:
        raise VersionedDocCError(
            f"missing external symbol graphs for {module['moduleName']}: {source_path}"
        )
    destination.mkdir(parents=True, exist_ok=True)
    copied = []
    for candidate in candidates:
        with candidate.open(encoding="utf-8") as source:
            graph = json.load(source)
        if graph.get("module", {}).get("name") != module["moduleName"]:
            continue
        output = destination / candidate.name
        if output.exists():
            raise VersionedDocCError(
                f"duplicate external symbol graph filename for {module['moduleName']}: {candidate.name}"
            )
        shutil.copy2(candidate, output)
        filter_symbol_graph(output, allowed_modules)
        rewrite_symbol_graph_locations(output, source_root)
        copied.append(output)
    if not copied:
        raise VersionedDocCError(
            f"no external symbol graphs for module {module['moduleName']} under {source_path}"
        )
    retain_symbol_graph_module(destination, module["moduleName"])
    print(
        f"{version['name']}: imported {len(copied)} symbol graph(s) for "
        f"{module['moduleName']} from {source_path}"
    )
    return copied


def build_version(
    package_root,
    config,
    version,
    commit,
    cache_root,
    logs_root,
    fingerprint,
    build_date,
    header_template,
    footer_template,
    docc_command,
):
    cache_entry = cache_root / version["name"]
    staging = cache_root / f".building-{version['name']}-{os.getpid()}"
    remove_tree(staging, cache_root, "cache staging directory")
    (staging / "symbols").mkdir(parents=True)
    (staging / "site").mkdir()
    (staging / "catalog").mkdir()
    print(f"Building {version['name']} ({commit[:8]})")
    platforms = []
    catalog_fallback_source_commit = None
    configured_catalog_path = version.get("catalogPath", config["catalogPath"])
    try:
        with PreparedSource(package_root, config, version, commit) as source_root:
            graph_root = staging / "symbols"
            if config["documentationOnly"]:
                modules = [
                    {
                        "moduleName": None,
                        "modulePath": config["modulePath"],
                        "catalogPath": configured_catalog_path,
                        "primary": True,
                    }
                ]
            else:
                modules = modules_for_version(config, version)
            module_source_routes = {
                module.get("moduleName"): module_source_route(
                    package_root,
                    config,
                    version,
                    source_root,
                    module,
                )
                for module in modules
            }
            module_graph_roots = {}
            if not config["documentationOnly"]:
                primary_graph_root = (
                    graph_root
                    if len(modules) == 1
                    else graph_root / f"00-{platform_slug(config['moduleName'])}"
                )
                platforms = build_symbol_graphs(
                    source_root, config, version, primary_graph_root, logs_root
                )
                module_graph_roots[config["moduleName"]] = primary_graph_root
                for index, module in enumerate(modules[1:], 1):
                    module_graph_root = (
                        graph_root
                        / f"{index:02d}-{platform_slug(module['moduleName'])}"
                    )
                    module_source_route_value = module_source_routes[
                        module["moduleName"]
                    ]
                    import_symbol_graphs(
                        package_root,
                        (
                            module_source_route_value["checkoutRoot"]
                            if module_source_route_value
                            else source_root
                        ),
                        module,
                        version,
                        commit,
                        module_graph_root,
                        set(config["allowedModules"]),
                    )
                    module_graph_roots[module["moduleName"]] = module_graph_root

            prepared_catalogs = []
            archives = []
            archives_root = staging / "archives"
            if len(modules) > 1:
                archives_root.mkdir()
            for index, module in enumerate(modules):
                module_name = module.get("moduleName")
                module_catalog_path = module.get("catalogPath")
                source_route = module_source_routes[module_name]
                module_source_root = (
                    source_route["checkoutRoot"] if source_route else source_root
                )
                edit_catalog_path = module_catalog_path or ""
                edit_reference = (
                    source_route["reference"]
                    if source_route
                    else source_reference(config, version)
                )
                edit_repository = (
                    source_route["repository"] if source_route else None
                )
                source_catalog = (
                    module_source_root / module_catalog_path
                    if module_catalog_path
                    else None
                )
                if source_catalog is not None and not source_catalog.is_dir():
                    if not module["primary"]:
                        raise VersionedDocCError(
                            f"missing DocC catalog for {module_name}: {source_catalog}"
                        )
                    fallback_catalog = package_root / config["catalogPath"]
                    if (
                        config.get("historicalCatalogFallback") == "current"
                        and fallback_catalog.is_dir()
                        and fallback_catalog.resolve() != source_catalog.resolve()
                    ):
                        source_catalog = fallback_catalog
                        catalog_fallback_source_commit = git(
                            package_root, "rev-parse", "HEAD"
                        )
                        edit_catalog_path = config["catalogPath"]
                        edit_reference = catalog_fallback_source_commit
                        print(
                            f"{version['name']}: using current DocC catalog "
                            f"({catalog_fallback_source_commit[:8]})"
                        )
                    else:
                        raise VersionedDocCError(
                            f"missing DocC catalog: {source_catalog}"
                        )
                catalog_name = (
                    source_catalog.name
                    if source_catalog is not None
                    else f"{module_name}.docc"
                )
                catalog = staged_catalog_path(
                    staging / "catalog",
                    catalog_name,
                    module_name,
                    index,
                    len(modules),
                )
                catalog.parent.mkdir(parents=True, exist_ok=True)
                if source_catalog is None:
                    catalog.mkdir()
                else:
                    shutil.copytree(source_catalog, catalog)
                (catalog / "header.html").write_text(
                    render_header(
                        header_template, config, version["name"], build_date
                    ),
                    encoding="utf-8",
                )
                append_custom_footer(
                    catalog,
                    render_footer(footer_template, config, version["name"]),
                )
                output = (
                    staging / "site"
                    if len(modules) == 1
                    else archives_root
                    / f"{index:02d}-{platform_slug(module_name)}.doccarchive"
                )
                docc = [*docc_command, "convert", str(catalog)]
                if module_name is not None:
                    docc.extend(
                        [
                            "--additional-symbol-graph-dir",
                            str(module_graph_roots[module_name]),
                        ]
                    )
                docc.extend(
                    [
                        "--transform-for-static-hosting",
                        "--output-path",
                        str(output),
                        "--hosting-base-path",
                        f"{config['hostingBasePath']}/{version['name']}",
                        "--default-code-listing-language",
                        "swift",
                        "--experimental-enable-custom-templates",
                        *config["doccArguments"],
                    ]
                )
                docc.extend(
                    source_service_arguments(
                        config,
                        version,
                        source_root,
                        module,
                        package_root,
                    )
                )
                log_module = platform_slug(module_name or config["modulePath"])
                run(
                    docc,
                    log_path=logs_root
                    / f"{version['name']}-{log_module}-docc.log",
                )
                if source_catalog is not None:
                    prepared_catalogs.append(
                        (
                            catalog,
                            edit_catalog_path,
                            edit_reference,
                            edit_repository,
                            Path(catalog_name).stem,
                        )
                    )
                archives.append(output)
            if len(archives) > 1:
                run(
                    [
                        *docc_command,
                        "merge",
                        *map(str, archives),
                        "--output-path",
                        str(staging / "site"),
                    ],
                    log_path=logs_root / f"{version['name']}-docc-merge.log",
                )
            if not (staging / "site" / "index.html").is_file():
                raise VersionedDocCError(f"DocC emitted no index for {version['name']}")
            for (
                catalog,
                edit_catalog_path,
                edit_reference,
                edit_repository,
                archive_identifier,
            ) in prepared_catalogs:
                inject_edit_metadata(
                    staging / "site",
                    catalog,
                    edit_catalog_path,
                    config,
                    version,
                    edit_reference,
                    archive_identifier,
                    source_repository=edit_repository,
                )
            theme_settings = staging / "site" / "theme-settings.json"
            if not theme_settings.exists():
                theme_settings.write_text("{}\n", encoding="utf-8")
    except Exception:
        remove_tree(staging, cache_root, "failed cache staging directory")
        raise

    remove_tree(staging / "catalog", staging, "temporary catalog")
    remove_tree(staging / "archives", staging, "temporary DocC archives")
    metadata = {
        "schemaVersion": 1,
        "generatorVersion": VERSION,
        "version": version["name"],
        "ref": version["ref"],
        "sourceCommit": commit,
        "catalogPath": configured_catalog_path,
        "buildDate": build_date,
        "buildFingerprint": fingerprint,
        "modules": configured_module_names(config, version),
        "platforms": (
            []
            if config["documentationOnly"]
            else (
                [platform["name"] for platform in platforms]
                if platforms
                else ["host"]
            )
        ),
    }
    if catalog_fallback_source_commit is not None:
        metadata["catalogFallbackSourceCommit"] = catalog_fallback_source_commit
    if config.get("sourceRepository"):
        metadata["sourceRepository"] = config["sourceRepository"].rstrip("/")
        metadata["sourceRef"] = source_reference(config, version)
    module_sources = {}
    for module in modules:
        route = module_source_routes[module.get("moduleName")]
        if route is None:
            continue
        module_identifier = module.get("moduleName") or module["modulePath"]
        module_sources[module_identifier] = {
            "repository": route["repository"],
            "ref": route["reference"],
        }
    if module_sources:
        metadata["moduleSources"] = module_sources
    (staging / "metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    remove_tree(cache_entry, cache_root, "version cache")
    staging.rename(cache_entry)
    print(f"Cached {version['name']} at {cache_entry}")


def version_options(versions, current):
    releases = [item["name"] for item in versions if item["name"] != "main"]
    latest_release = releases[0] if releases else None
    options = []
    for item in versions:
        name = item["name"]
        label = "main (Development)" if name == "main" else name
        if name == latest_release:
            label = f"{name} (Latest Release)"
        selected = " selected" if name == current else ""
        options.append(
            f'        <option value="{html.escape(name)}"{selected}>{html.escape(label)}</option>'
        )
    return "\n".join(options)


def finalize_site(site_root, versions, current):
    replacement = version_options(versions, current)
    replacements = 0
    for html_path in site_root.rglob("*.html"):
        contents = html_path.read_text(encoding="utf-8")
        count = contents.count(OPTIONS_TOKEN)
        if count:
            html_path.write_text(contents.replace(OPTIONS_TOKEN, replacement), encoding="utf-8")
            replacements += count
    if not replacements:
        raise VersionedDocCError(f"no version selector token found under {site_root}")
    print(f"{current}: injected {len(versions)} versions into {replacements} templates")


def documentation_module(url):
    marker = "/documentation/"
    if not isinstance(url, str) or marker not in url:
        return None
    remainder = url.split(marker, 1)[1]
    return remainder.split("/", 1)[0]


def write_json(path, value):
    path.write_text(
        json.dumps(value, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def prune_site_to_module(site_root, module_path, allowed_module_paths=()):
    expected = {
        module.casefold() for module in (module_path, *allowed_module_paths)
    }
    documentation_entries = set(expected)
    if (site_root / "data" / "documentation.json").is_file():
        documentation_entries.add("index.html")
    removed_entries = 0

    for root, keep_names in (
        (site_root / "documentation", documentation_entries),
        (
            site_root / "data" / "documentation",
            expected | {f"{module}.json" for module in expected},
        ),
    ):
        if not root.is_dir():
            continue
        for child in root.iterdir():
            if child.name.casefold() in keep_names:
                continue
            ensure_safe_child(child, root, "dependency documentation")
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
            removed_entries += 1

    index_path = site_root / "index" / "index.json"
    if index_path.is_file():
        with index_path.open(encoding="utf-8") as source:
            index = json.load(source)
        expected_paths = {
            f"/documentation/{module}".casefold() for module in expected
        }
        languages = index.get("interfaceLanguages", {})
        for language, modules in languages.items():
            retained = []
            for module in modules:
                path = module.get("path", "").rstrip("/").casefold()
                if path == "/documentation":
                    module["children"] = [
                        child
                        for child in module.get("children", [])
                        if child.get("path", "").rstrip("/").casefold()
                        in expected_paths
                    ]
                    if module["children"]:
                        retained.append(module)
                elif path in expected_paths:
                    retained.append(module)
            languages[language] = retained
        write_json(index_path, index)

    indexing_records_path = site_root / "indexing-records.json"
    if indexing_records_path.is_file():
        with indexing_records_path.open(encoding="utf-8") as source:
            records = json.load(source)
        records = [
            record
            for record in records
            if (
                (module := documentation_module(
                    record.get("location", {}).get("reference", {}).get("url")
                ))
                is None
                or module.casefold() in expected
            )
        ]
        write_json(indexing_records_path, records)

    linkable_entities_path = site_root / "linkable-entities.json"
    if linkable_entities_path.is_file():
        with linkable_entities_path.open(encoding="utf-8") as source:
            entities = json.load(source)
        entities = [
            entity
            for entity in entities
            if (
                (module := documentation_module(entity.get("referenceURL"))) is None
                or module.casefold() in expected
            )
        ]
        write_json(linkable_entities_path, entities)

    print(f"{module_path}: removed {removed_entries} dependency documentation entries")


def root_index(config):
    base = config["hostingBasePath"]
    default = config["defaultVersion"]
    module_path = config["modulePath"]
    target = f"{base}/{default}/documentation/{module_path}/"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="0; url={html.escape(target)}"><link rel="canonical" href="{html.escape(target)}">
<title>{html.escape(config['projectName'])} Documentation</title>
<script>window.location.replace({json.dumps(target)} + window.location.search + window.location.hash);</script>
</head><body><p>Opening <a href="{html.escape(target)}">{html.escape(config['projectName'])} documentation</a>.</p></body></html>\n"""


def github_pages_fallback(config):
    base = config["hostingBasePath"]
    default = config["defaultVersion"]
    module_path = config["modulePath"]
    legacy_root = f"{base}/documentation"
    versioned_root = f"{base}/{default}/documentation"
    documentation_root = f"{versioned_root}/{module_path}/"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><title>Page moved | {html.escape(config['projectName'])} Documentation</title>
<script data-versioned-docc-pages-fallback>
(() => {{
  const legacyRoot = {json.dumps(legacy_root)};
  const versionedRoot = {json.dumps(versioned_root)};
  const path = window.location.pathname;
  if (path === legacyRoot || path.startsWith(legacyRoot + "/")) {{
    const suffix = path.slice(legacyRoot.length) || "/";
    const target = versionedRoot + suffix + window.location.search + window.location.hash;
    const canonical = document.createElement("link");
    canonical.rel = "canonical";
    canonical.href = target;
    document.head.appendChild(canonical);
    window.location.replace(target);
  }}
}})();
</script></head><body><h1>Page not found</h1>
<p>This documentation URL may have moved. Open <a href="{html.escape(documentation_root)}">{html.escape(config['projectName'])} documentation</a>.</p>
</body></html>\n"""


def deployment_root(output_path, config):
    return output_path.parent if config["hostingBasePath"] else output_path


def write_legacy_routing_files(output_path, config):
    redirect = (
        f"{config['hostingBasePath']}/documentation/* "
        f"{config['hostingBasePath']}/{config['defaultVersion']}/documentation/:splat 301\n"
    )
    fallback = github_pages_fallback(config)
    # output_path is convenient for workflows that upload the site directly.
    # A project hostingBasePath is also represented as a physical child of the
    # deploy root during local preview; a domain-root site has no such parent.
    roots = [output_path]
    deploy_root = deployment_root(output_path, config)
    if deploy_root != output_path:
        roots.append(deploy_root)
    for root in roots:
        (root / "_redirects").write_text(redirect, encoding="utf-8")
        (root / "404.html").write_text(fallback, encoding="utf-8")


def assemble(package_root, config, versions, cache_root, output_path, build_date):
    output_parent = output_path.parent
    output_parent.mkdir(parents=True, exist_ok=True)
    remove_tree(output_path, output_parent, "assembled output")
    output_path.mkdir(parents=True)
    manifest = {
        "schemaVersion": 1,
        "generatorVersion": VERSION,
        "defaultVersion": config["defaultVersion"],
        "assembledAt": build_date,
        "versions": [],
    }
    for version in versions:
        cache_entry = cache_root / version["name"]
        with (cache_entry / "metadata.json").open(encoding="utf-8") as source:
            metadata = json.load(source)
        manifest["versions"].append(
            {
                "name": version["name"],
                "path": f"{config['hostingBasePath']}/{version['name']}/",
                "buildDate": metadata["buildDate"],
                "sourceCommit": metadata["sourceCommit"],
                "platforms": metadata.get("platforms", ["host"]),
            }
        )
        version_output = output_path / version["name"]
        shutil.copytree(cache_entry / "site", version_output)
        # Assembly also sanitizes older local and OCI caches created before
        # dependency symbol graphs were excluded from DocC conversion.
        prune_site_to_module(
            version_output,
            config["modulePath"],
            [*config["allowedModules"], *configured_module_paths(config, version)],
        )
        finalize_site(version_output, versions, version["name"])
    (output_path / "versions.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output_path / "index.html").write_text(root_index(config), encoding="utf-8")
    write_legacy_routing_files(output_path, config)
    (output_path / ".nojekyll").touch()

    if not changes_enabled(config):
        print(f"Versioned documentation assembled at {output_path}")
        return

    api_script = Path(__file__).with_name("api_changes.py")
    api_command = [
        sys.executable,
        str(api_script),
        "--output-root",
        str(output_path),
        "--hosting-base-path",
        config["hostingBasePath"],
        "--default-version",
        config["defaultVersion"],
        "--build-date",
        build_date,
        "--project-name",
        config["projectName"],
        "--module-path",
        config["modulePath"],
        "--page-size",
        str(config["apiChanges"]["pageSize"]),
    ]
    if site_ui_value(config, "showStar"):
        api_command.extend(
            ["--star-repository-url", config["sourceRepository"].rstrip("/")]
        )
    if site_ui_value(config, "showPoweredBy"):
        api_command.extend(
            ["--powered-by-url", VERSIONED_DOCC_REPOSITORY]
        )
    for version in versions:
        if not config["documentationOnly"]:
            for module_name in configured_module_names(config, version):
                graph_paths = module_symbol_graph_paths(
                    cache_root / version["name"] / "symbols", module_name
                )
                if not graph_paths:
                    raise VersionedDocCError(
                        f"missing symbol graphs for {version['name']} ({module_name})"
                    )
                for graph_path in graph_paths:
                    api_command.extend(
                        ["--symbol-graph", f"{version['name']}={graph_path}"]
                    )
        if config["articleChanges"]["enabled"]:
            api_command.extend(
                [
                    "--article-root",
                    f"{version['name']}={output_path / version['name'] / 'data' / 'documentation'}",
                ]
            )
    run(api_command)
    print(f"Versioned documentation assembled at {output_path}")


def build_command(arguments):
    package_root = resolve_path(Path.cwd(), arguments.package_path)
    config, config_path = load_config(package_root, arguments.config)
    if (
        not config["documentationOnly"]
        and not (package_root / "Package.swift").is_file()
    ):
        raise VersionedDocCError(f"not a Swift package: {package_root}")
    versions = configured_versions(package_root, config)
    output_path = resolve_path(package_root, arguments.output or config["outputPath"])
    cache_root = resolve_path(package_root, arguments.cache or config["cachePath"])
    docs_root = package_root / ".docs"
    ensure_safe_child(output_path, package_root, "output path")
    ensure_safe_child(cache_root, package_root, "cache path")
    cache_root.mkdir(parents=True, exist_ok=True)
    logs_root = docs_root / "logs" / "versioned-docc"
    logs_root.mkdir(parents=True, exist_ok=True)
    build_date = arguments.build_date or os.environ.get("DOCS_BUILD_DATE") or default_build_date()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", build_date):
        raise VersionedDocCError("build date must use YYYY-MM-DD")
    header_template = Path(__file__).with_name("header.html").read_text(encoding="utf-8")
    footer_template = Path(__file__).with_name("footer.html").read_text(encoding="utf-8")
    docc_command, docc_binary = find_docc()
    base_fingerprint = build_fingerprint(
        config, docc_binary, header_template, footer_template
    )
    oci_cache = config.get("ociCache")
    if arguments.publish_oci_cache and not oci_cache:
        raise VersionedDocCError("--publish-oci-cache requires ociCache configuration")
    if arguments.publish_oci_cache and arguments.no_oci_cache:
        raise VersionedDocCError("--publish-oci-cache cannot be combined with --no-oci-cache")
    oras = None
    reported_missing_oras = False
    print(f"VersionedDocC {VERSION}")
    print(f"  Config: {config_path}")
    print(f"  Versions: {' '.join(item['name'] for item in versions)}")
    print(f"  Cache: {cache_root}")
    print(f"  Output: {output_path}")
    if oci_cache:
        print(f"  OCI cache: {oci_cache['repository']}")
    for version in versions:
        fingerprint = version_cache_fingerprint(
            base_fingerprint, config, version
        )
        commit = git(package_root, "rev-parse", f"{version['ref']}^{{commit}}")
        cache_entry = cache_root / version["name"]
        cache_hit = not arguments.rebuild and cache_valid(
            cache_entry,
            commit,
            fingerprint,
            configured_module_names(config, version),
        )
        uses_remote = (
            oci_cache
            and not arguments.no_oci_cache
            and uses_oci_cache(config, version)
        )
        if (
            not cache_hit
            and not arguments.rebuild
            and uses_remote
            and oci_cache.get("pull", True)
        ):
            if oras is None:
                oras = find_oras()
            if oras is None:
                if not reported_missing_oras:
                    print("OCI cache unavailable: install oras or set VERSIONED_DOCC_ORAS")
                    reported_missing_oras = True
            else:
                cache_hit = restore_oci_cache(
                    oras,
                    config,
                    version,
                    commit,
                    fingerprint,
                    cache_root,
                )
        if cache_hit:
            print(f"Cache hit: {version['name']} ({commit[:8]})")
        else:
            if arguments.assemble_only:
                raise VersionedDocCError(
                    f"cache miss for {version['name']} in assemble-only mode"
                )
            build_version(
                package_root,
                config,
                version,
                commit,
                cache_root,
                logs_root,
                fingerprint,
                build_date,
                header_template,
                footer_template,
                docc_command,
            )
        if arguments.publish_oci_cache and uses_remote:
            if oras is None:
                oras = find_oras()
            if oras is None:
                raise VersionedDocCError(
                    "--publish-oci-cache requires oras; install it or set VERSIONED_DOCC_ORAS"
                )
            publish_oci_cache(
                oras,
                config,
                version,
                commit,
                fingerprint,
                cache_root,
            )
    assemble(package_root, config, versions, cache_root, output_path, build_date)
    preview_suffix = (
        "changes/"
        if changes_enabled(config)
        else f"documentation/{config['modulePath']}/"
    )
    print(
        f"Preview: http://127.0.0.1:{arguments.preview_port}"
        f"{config['hostingBasePath']}/{config['defaultVersion']}/{preview_suffix}"
    )


def preview_command(arguments):
    package_root = resolve_path(Path.cwd(), arguments.package_path)
    config, _ = load_config(package_root, arguments.config)
    output_path = resolve_path(package_root, arguments.output or config["outputPath"])
    web_root = deployment_root(output_path, config)
    if not output_path.is_dir():
        raise VersionedDocCError(f"missing assembled site: {output_path}")
    legacy_prefix = f"{config['hostingBasePath']}/documentation/"
    versioned_prefix = f"{config['hostingBasePath']}/{config['defaultVersion']}/documentation/"

    class Handler(SimpleHTTPRequestHandler):
        def redirect_legacy(self):
            path, separator, query = self.path.partition("?")
            if not path.startswith(legacy_prefix):
                return False
            target = versioned_prefix + path[len(legacy_prefix) :]
            if separator:
                target += "?" + query
            self.send_response(301)
            self.send_header("Location", target)
            self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()
            return True

        def do_GET(self):
            if not self.redirect_legacy():
                super().do_GET()

        def do_HEAD(self):
            if not self.redirect_legacy():
                super().do_HEAD()

    handler = partial(Handler, directory=str(web_root))
    server = ThreadingHTTPServer((arguments.bind, arguments.port), handler)
    print(f"Serving {web_root} at http://{arguments.bind}:{arguments.port}")
    print(f"Wildcard redirect: {legacy_prefix}* -> {versioned_prefix}:splat")
    server.serve_forever()


def resolve_versions_command(arguments):
    package_root = resolve_path(Path.cwd(), arguments.package_path)
    config, _ = load_config(package_root, arguments.config)
    output_path = resolve_path(package_root, arguments.output)
    ensure_safe_child(output_path, package_root, "resolved versions output")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(resolved_versions(package_root, config), indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Resolved documentation versions at {output_path}")


def parse_arguments():
    parser = argparse.ArgumentParser(
        prog="versioned-docc",
        description="Build, compare, cache, and host versioned Swift-DocC documentation.",
    )
    parser.add_argument("--version", action="version", version=VERSION)
    subparsers = parser.add_subparsers(dest="command")
    build = subparsers.add_parser("build", help="Build cache misses and assemble the site")
    build.add_argument("--package-path", default=".")
    build.add_argument("--config", default=DEFAULT_CONFIG)
    build.add_argument("--output")
    build.add_argument("--cache")
    build.add_argument("--build-date")
    build.add_argument("--assemble-only", action="store_true")
    build.add_argument("--rebuild", action="store_true")
    build.add_argument(
        "--no-oci-cache",
        action="store_true",
        help="do not restore or publish configured OCI cache artifacts",
    )
    build.add_argument(
        "--publish-oci-cache",
        action="store_true",
        help="publish eligible version caches after validating or building them",
    )
    build.add_argument("--preview-port", type=int, default=8766)
    preview = subparsers.add_parser("preview", help="Serve an assembled site with wildcard redirects")
    preview.add_argument("--package-path", default=".")
    preview.add_argument("--config", default=DEFAULT_CONFIG)
    preview.add_argument("--output")
    preview.add_argument("--bind", default="127.0.0.1")
    preview.add_argument("--port", type=int, default=8766)
    resolve_versions = subparsers.add_parser(
        "resolve-versions",
        help="Write the configured documentation versions and exact commits as JSON",
    )
    resolve_versions.add_argument("--package-path", default=".")
    resolve_versions.add_argument("--config", default=DEFAULT_CONFIG)
    resolve_versions.add_argument("--output", required=True)
    arguments = parser.parse_args()
    if arguments.command is None:
        arguments = parser.parse_args(["build", *sys.argv[1:]])
    return arguments


def main():
    arguments = parse_arguments()
    if arguments.command == "preview":
        preview_command(arguments)
    elif arguments.command == "resolve-versions":
        resolve_versions_command(arguments)
    else:
        build_command(arguments)


if __name__ == "__main__":
    try:
        main()
    except (VersionedDocCError, OSError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
