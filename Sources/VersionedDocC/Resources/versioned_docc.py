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


VERSION = "0.0.9"
DEFAULT_CONFIG = ".vdc.json"
# Keep this stable across releases that only change assembly, routing, or the
# command interface. Bump it only when the per-version DocC cache contents must
# be regenerated. Its initial value preserves 0.0.1 cache fingerprints.
BUILD_CACHE_REVISION = "0.0.1"
SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")
LATEST_RELEASE_STRATEGIES = {"majorMinor", "semanticVersion", "tagDate"}
OPTIONS_TOKEN = "__VERSIONED_DOCC_VERSION_OPTIONS__"
OCI_ARTIFACT_TYPE = "application/vnd.openswiftuiproject.versioned-docc.cache.v1"
OCI_CONFIG_TYPE = "application/vnd.openswiftuiproject.versioned-docc.cache.config.v1+json"
OCI_LAYER_TYPE = "application/vnd.openswiftuiproject.versioned-docc.cache.layer.v1.tar+gzip"
OCI_ARCHIVE_NAME = "versioned-docc-cache.tar.gz"


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
    required = ["projectName", "moduleName", "targetName", "catalogPath", "hostingBasePath"]
    missing = [key for key in required if not config.get(key)]
    if missing:
        raise VersionedDocCError(f"missing configuration keys: {', '.join(missing)}")
    base_path = "/" + config["hostingBasePath"].strip("/")
    if not re.fullmatch(r"(?:/[A-Za-z0-9._-]+)+", base_path):
        raise VersionedDocCError(f"invalid hostingBasePath: {base_path}")
    config["hostingBasePath"] = base_path
    config.setdefault("modulePath", config["moduleName"].lower())
    config.setdefault("defaultVersion", "main")
    config.setdefault("outputPath", f".docs/build/versioned-site{base_path}")
    config.setdefault("cachePath", ".docs/cache/versioned-docc")
    config.setdefault("buildArguments", ["--disable-index-store"])
    config.setdefault("doccArguments", ["--emit-digest"])
    config.setdefault("environment", {})
    config.setdefault("localDependencies", {})
    config.setdefault("allowedModules", [config["moduleName"]])
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
        normalized.append({"name": name, "ref": ref})
    if config["defaultVersion"] not in names:
        raise VersionedDocCError(
            f"defaultVersion {config['defaultVersion']} isn't in the version list"
        )
    if len(normalized) < 2:
        raise VersionedDocCError("at least two documentation versions are required")
    return normalized


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


def filter_symbol_graph(path, allowed_modules):
    with path.open(encoding="utf-8") as source:
        graph = json.load(source)
    original_symbols = graph.get("symbols", [])
    symbols = [
        symbol
        for symbol in original_symbols
        if swift_module(symbol.get("identifier", {}).get("precise", "")) in allowed_modules
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


def render_header(template, config, version, build_date):
    base = config["hostingBasePath"]
    module_path = config["modulePath"]
    replacements = {
        "__VERSIONED_DOCC_PROJECT_NAME__": config["projectName"],
        "__VERSIONED_DOCC_HOME_PATH__": f"{base}/{version}/documentation/{module_path}/",
        "__VERSIONED_DOCC_CHANGES_PATH__": f"{base}/{config['defaultVersion']}/changes/",
        "__VERSIONED_DOCC_BUILD_DATE__": build_date,
        "__VERSIONED_DOCC_CURRENT_VERSION__": version,
    }
    for token, value in replacements.items():
        template = template.replace(token, value)
    return template


def build_fingerprint(config, docc_binary, header_template):
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
        "target": config["targetName"],
        "module": config["moduleName"],
        "catalog": config["catalogPath"],
        "environment": config["environment"],
        "buildArguments": config["buildArguments"],
        "doccArguments": config["doccArguments"],
        "symbolGraph": config["symbolGraph"],
        "allowedModules": config["allowedModules"],
        "historicalCatalogFallback": config.get("historicalCatalogFallback"),
    }
    return sha256_bytes(json.dumps(payload, sort_keys=True).encode())


def cache_valid(cache_entry, commit, fingerprint, module_name):
    metadata_path = cache_entry / "metadata.json"
    site_path = cache_entry / "site" / "index.html"
    graph_paths = module_symbol_graph_paths(cache_entry / "symbols", module_name)
    if not metadata_path.is_file() or not graph_paths or not site_path.is_file():
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
        if not cache_valid(staging, commit, fingerprint, config["moduleName"]):
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
    docc_command,
):
    cache_entry = cache_root / version["name"]
    staging = cache_root / f".building-{version['name']}-{os.getpid()}"
    remove_tree(staging, cache_root, "cache staging directory")
    (staging / "symbols").mkdir(parents=True)
    (staging / "site").mkdir()
    (staging / "catalog").mkdir()
    print(f"Building {version['name']} ({commit[:8]})")
    try:
        with PreparedSource(package_root, config, version, commit) as source_root:
            graph_root = staging / "symbols"
            platforms = configured_symbol_graph_platforms(config)
            graph_builds = (
                [
                    (
                        platform,
                        graph_root
                        / f"{index:02d}-{platform_slug(platform['name'])}",
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
                    # Command plugins already run inside SwiftPM's sandbox. A
                    # nested swift build can't apply a second sandbox profile.
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
                    build_command.extend(
                        ["-Xswiftc", "-emit-extension-block-symbols"]
                    )
                if config["symbolGraph"].get("skipProtocolImplementations", True):
                    # Force the symbol-graph option through SwiftPM's driver. Passing
                    # it as a plain Swift driver option doesn't reach the frontend
                    # emit-module job with current Apple Swift toolchains.
                    build_command.extend(
                        [
                            "-Xswiftc",
                            "-Xfrontend",
                            "-Xswiftc",
                            "-skip-protocol-implementations",
                        ]
                    )
                run(
                    build_command,
                    environment=config["environment"],
                    log_path=logs_root
                    / f"{version['name']}{log_suffix}-swift-build.log",
                )
                graph_path = graph_directory / f"{config['moduleName']}.symbols.json"
                if not graph_path.is_file():
                    raise VersionedDocCError(f"missing symbol graph: {graph_path}")
                filter_symbol_graph(graph_path, set(config["allowedModules"]))
                retain_symbol_graph_module(graph_directory, config["moduleName"])

            source_catalog = source_root / config["catalogPath"]
            catalog_fallback_source_commit = None
            if not source_catalog.is_dir():
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
                    print(
                        f"{version['name']}: using current DocC catalog "
                        f"({catalog_fallback_source_commit[:8]})"
                    )
                else:
                    raise VersionedDocCError(
                        f"missing DocC catalog: {source_catalog}"
                    )
            catalog = staging / "catalog" / source_catalog.name
            shutil.copytree(source_catalog, catalog)
            (catalog / "header.html").write_text(
                render_header(header_template, config, version["name"], build_date),
                encoding="utf-8",
            )
            docc = [
                *docc_command,
                "convert",
                str(catalog),
                "--additional-symbol-graph-dir",
                str(graph_root),
                "--transform-for-static-hosting",
                "--output-path",
                str(staging / "site"),
                "--hosting-base-path",
                f"{config['hostingBasePath']}/{version['name']}",
                "--default-code-listing-language",
                "swift",
                "--experimental-enable-custom-templates",
                *config["doccArguments"],
            ]
            source_repository = config.get("sourceRepository")
            if source_repository:
                source_ref = version.get("sourceRef", version["ref"])
                if source_ref == "HEAD":
                    source_ref = config.get("developmentSourceRef", "main")
                docc.extend(
                    [
                        "--source-service",
                        "github",
                        "--source-service-base-url",
                        f"{source_repository.rstrip('/')}/blob/{source_ref}",
                        "--checkout-path",
                        str(source_root),
                    ]
                )
            run(docc, log_path=logs_root / f"{version['name']}-docc.log")
            if not (staging / "site" / "index.html").is_file():
                raise VersionedDocCError(f"DocC emitted no index for {version['name']}")
            theme_settings = staging / "site" / "theme-settings.json"
            if not theme_settings.exists():
                theme_settings.write_text("{}\n", encoding="utf-8")
    except Exception:
        remove_tree(staging, cache_root, "failed cache staging directory")
        raise

    remove_tree(staging / "catalog", staging, "temporary catalog")
    metadata = {
        "schemaVersion": 1,
        "generatorVersion": VERSION,
        "version": version["name"],
        "ref": version["ref"],
        "sourceCommit": commit,
        "buildDate": build_date,
        "buildFingerprint": fingerprint,
        "platforms": [platform["name"] for platform in platforms]
        if platforms
        else ["host"],
    }
    if catalog_fallback_source_commit is not None:
        metadata["catalogFallbackSourceCommit"] = catalog_fallback_source_commit
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
    removed_entries = 0

    for root, keep_names in (
        (site_root / "documentation", expected),
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
            languages[language] = [
                module
                for module in modules
                if module.get("path", "").rstrip("/").casefold()
                in expected_paths
            ]
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


def write_legacy_routing_files(output_path, config):
    redirect = (
        f"{config['hostingBasePath']}/documentation/* "
        f"{config['hostingBasePath']}/{config['defaultVersion']}/documentation/:splat 301\n"
    )
    fallback = github_pages_fallback(config)
    # output_path is convenient for workflows that upload the project site
    # directly. output_path.parent is the deploy root used when hostingBasePath
    # is represented as a physical directory, as in the local preview.
    for root in (output_path, output_path.parent):
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
            config["allowedModules"],
        )
        finalize_site(version_output, versions, version["name"])
    (output_path / "versions.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output_path / "index.html").write_text(root_index(config), encoding="utf-8")
    write_legacy_routing_files(output_path, config)
    (output_path / ".nojekyll").touch()

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
    for version in versions:
        graph_paths = module_symbol_graph_paths(
            cache_root / version["name"] / "symbols", config["moduleName"]
        )
        if not graph_paths:
            raise VersionedDocCError(
                f"missing symbol graphs for {version['name']} ({config['moduleName']})"
            )
        for graph_path in graph_paths:
            api_command.extend(
                ["--symbol-graph", f"{version['name']}={graph_path}"]
            )
    run(api_command)
    print(f"Versioned documentation assembled at {output_path}")


def build_command(arguments):
    package_root = resolve_path(Path.cwd(), arguments.package_path)
    if not (package_root / "Package.swift").is_file():
        raise VersionedDocCError(f"not a Swift package: {package_root}")
    config, config_path = load_config(package_root, arguments.config)
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
    docc_command, docc_binary = find_docc()
    fingerprint = build_fingerprint(config, docc_binary, header_template)
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
        commit = git(package_root, "rev-parse", f"{version['ref']}^{{commit}}")
        cache_entry = cache_root / version["name"]
        cache_hit = not arguments.rebuild and cache_valid(
            cache_entry, commit, fingerprint, config["moduleName"]
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
    print(
        f"Preview: http://127.0.0.1:{arguments.preview_port}"
        f"{config['hostingBasePath']}/{config['defaultVersion']}/changes/"
    )


def preview_command(arguments):
    package_root = resolve_path(Path.cwd(), arguments.package_path)
    config, _ = load_config(package_root, arguments.config)
    output_path = resolve_path(package_root, arguments.output or config["outputPath"])
    web_root = output_path.parent
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
    arguments = parser.parse_args()
    if arguments.command is None:
        arguments = parser.parse_args(["build", *sys.argv[1:]])
    return arguments


def main():
    arguments = parse_arguments()
    if arguments.command == "preview":
        preview_command(arguments)
    else:
        build_command(arguments)


if __name__ == "__main__":
    try:
        main()
    except (VersionedDocCError, OSError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
