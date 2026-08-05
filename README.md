# VersionedDocC

VersionedDocC builds Swift-DocC documentation for a development branch and
published releases, stores each version as an immutable artifact, and assembles
a version-aware static website. Swift packages also get public API comparisons
from their symbol graphs; standalone DocC catalogs can be published without a
`Package.swift` file.

VersionedDocC is an orchestration layer around the Swift toolchain's `swift`
and `docc` executables. It does not fork Swift-DocC or Swift-DocC Render.

## Features

- Stable `/<project>/<version>/documentation/...` URLs.
- A custom DocC header with version selection and UTC build date.
- Adjacent-version API Changes generated from real public symbol graphs.
- Optional adjacent-version article changes generated from stable DocC render content.
- `-skip-protocol-implementations` to avoid inherited protocol-extension
  members being repeated for every conforming type.
- Immutable per-version caches with source, toolchain, renderer, and build
  configuration fingerprints.
- Optional OCI release caches that restore and publish independent immutable
  artifacts through ORAS-compatible registries such as GHCR.
- Exact historical local dependencies from each tag's `Package.resolved`.
- A single provider-style wildcard redirect plus a GitHub Pages `404.html`
  fallback from legacy documentation URLs to the configured default version.
- A SwiftPM command plugin, standalone executable, composite GitHub Action,
  and reusable Pages workflow.
- Native documentation-only mode for repositories that contain a standalone
  `.docc` catalog but no Swift package or symbol graphs.

## Examples

- **Swift Book:** [GitHub repository](https://github.com/DocCLab/swift-book)
  · [Live versioned documentation](https://docclab.github.io/swift-book/main/documentation/the-swift-programming-language/)
- **Swift Syntax:** [GitHub repository](https://github.com/DocCLab/swift-syntax)
  · [Live versioned documentation](https://docclab.github.io/swift-syntax/main/documentation/swiftsyntax/)

## Agent skill

This repository includes an
[`adopt-versioned-docc`](skills/adopt-versioned-docc/SKILL.md) skill for
Codex and other Agent Skills-compatible tools. Install or copy the skill
directory into your agent's skill location, then ask it to adapt an existing
Swift package:

```text
Use $adopt-versioned-docc to add versioned, multi-platform DocC publishing to
this Swift package.
```

The skill inspects the package, release tags, DocC catalog, supported platforms,
and existing CI before generating `.vdc.json` and a publishing workflow. It also
guides local validation, Pages verification, and optional GHCR cache setup.

## Configuration

Add `.vdc.json` to the package root:

```json
{
  "$schema": "https://raw.githubusercontent.com/DocCLab/VersionedDocC/0.0.12/Schema/VersionedDocC.schema.json",
  "schemaVersion": 1,
  "projectName": "ExampleKit",
  "moduleName": "ExampleKit",
  "modulePath": "examplekit",
  "targetName": "ExampleKit",
  "catalogPath": "Sources/ExampleKit/ExampleKit.docc",
  "hostingBasePath": "/ExampleKit",
  "defaultVersion": "main",
  "releasePolicy": {
    "latest": 2,
    "latestStrategy": "majorMinor",
    "development": { "name": "main", "ref": "HEAD" }
  },
  "sourceRepository": "https://github.com/Example/ExampleKit",
  "ociCache": {
    "repository": "ghcr.io/example/examplekit-docc-cache"
  },
  "symbolGraph": {
    "minimumAccessLevel": "public",
    "skipProtocolImplementations": true,
    "defaultPlatform": "iOS",
    "platforms": [
      {
        "name": "iOS",
        "triple": "arm64-apple-ios",
        "sdk": "iphoneos"
      },
      {
        "name": "macOS",
        "triple": "arm64-apple-macosx",
        "sdk": "macosx"
      }
    ]
  },
  "apiChanges": {
    "pageSize": 10
  }
}
```

For a repository that contains only a standalone DocC catalog, set
`documentationOnly` and provide the catalog's stable documentation route with
`modulePath`. `moduleName`, `targetName`, symbol graph settings, and API Changes
aren't used:

```json
{
  "$schema": "https://raw.githubusercontent.com/DocCLab/VersionedDocC/0.0.12/Schema/VersionedDocC.schema.json",
  "schemaVersion": 1,
  "documentationOnly": true,
  "projectName": "swift-book",
  "modulePath": "the-swift-programming-language",
  "catalogPath": "TSPL.docc",
  "hostingBasePath": "/swift-book",
  "defaultVersion": "main",
  "articleChanges": {
    "enabled": true
  },
  "versions": [
    { "name": "main", "ref": "HEAD", "sourceRef": "main" },
    { "name": "6.3", "ref": "swift-6.3-fcs" },
    { "name": "6.2.3", "ref": "swift-6.2.3-fcs" }
  ],
  "sourceRepository": "https://github.com/swiftlang/swift-book"
}
```

Documentation-only caches contain the converted DocC site and metadata but no
symbol graphs. Article changes are disabled by default, so their header normally
omits the Changes link and assembly skips comparison generation. Set
`articleChanges.enabled` to `true` to compare authored articles between adjacent
versions. The generated Changes page lists added, modified, and removed articles
and includes a bounded unified content diff for modifications.

Article comparison fingerprints only each article's title, role, abstract,
rendered content, and topic sections. It excludes DocC references and
source-service metadata so a changed source URL or linked-page metadata doesn't
mark unrelated articles as modified. Enabling article changes also works for
normal Swift packages, where article and public API changes share one dashboard.

The CLI, SwiftPM plugin, composite action, and reusable workflow discover this
file automatically. Use `--config` or the `config` action input only for a
nonstandard path.

`latest` controls how many releases are selected. `latestStrategy` supports
three policies:

- `majorMinor` (default) selects the newest distinct `major.minor` series and
  keeps only the highest patch from each series. For example, `latest: 2`
  selects `0.20.1` and `0.19.3`, not both `0.20.1` and `0.20.0`.
- `semanticVersion` selects the highest semantic-version tags, including
  multiple patches from one series. For example, it can select `0.20.1` and
  `0.20.0`.
- `tagDate` selects tags by Git creator date, newest first. If `0.19.1` is
  created after `0.20.1`, `latest: 2` selects `0.19.1` and `0.20.1`.

For annotated tags, `tagDate` uses the tagger date; for lightweight tags, Git
uses the tagged commit's date.
`pinned` adds stable comparison baselines without duplicating a release series
already selected by `latest` under `majorMinor`. With another strategy, only an
exact duplicate is omitted. An explicit `versions` array remains exact and is
not grouped by release series. The example publishes `main` and the newest
release series.
API Changes pages show 10 entries per page by default. Consumers can override
the presentation-only value with `apiChanges.pageSize`; changing it reassembles
the site without invalidating version documentation caches.

### Multi-platform symbol graphs

When `symbolGraph.platforms` is present, VersionedDocC builds the target once
per configured platform, stores the resulting graphs in separate platform
directories, and passes their common parent directory to a single DocC
conversion. DocC then produces one archive containing the union of the API and
platform availability from every graph.

`defaultPlatform` must match a platform `name`. VersionedDocC processes that
graph first and uses its declaration as the primary representation of a symbol
shared by multiple platforms on the API Changes page. Platform-only symbols are
included from every configured graph. For Apple UI packages, iOS is a useful
primary platform and macOS a useful secondary platform; other packages should
choose the platform that represents their largest consumer surface.

`triple` is passed to `swift build --triple`. `sdk` can be an absolute SDK path
or an `xcrun --sdk` identifier such as `iphoneos` or `macosx`. Cross-platform
Swift SDK bundles can instead be selected with `swiftSDK`, and each platform can
append its own `buildArguments` array. Package deployment targets continue to
come from the package manifest when the triple does not include an OS version.

If `symbolGraph.platforms` is omitted, VersionedDocC preserves its original
single build on the current host platform. This backward-compatible default
also avoids invalidating existing single-platform release caches. Adding or
changing platform configuration is part of the immutable cache fingerprint, so
the affected version caches are rebuilt once.

Use an explicit `versions` array when a site needs fixed release snapshots:

```json
"versions": [
  { "name": "main", "ref": "HEAD", "sourceRef": "main" },
  { "name": "0.19.0", "ref": "0.19.0" },
  { "name": "0.18.0", "ref": "0.18.0" }
]
```

`localDependencies` maps SwiftPM package identities to canonical local Git
repositories. VersionedDocC creates temporary shared clones at the exact
revisions recorded in the selected source tag's `Package.resolved` file.
Nested SwiftPM builds disable their own sandbox because the command plugin is
already constrained by SwiftPM's outer sandbox.

Packages that added a DocC catalog after earlier releases can opt into a
one-time historical fallback:

```json
"historicalCatalogFallback": "current"
```

On a release cache miss, VersionedDocC uses the current checkout's catalog when
the selected tag has none and records the fallback source commit in cache
metadata. The catalog contents intentionally do not invalidate that release
after its cache is published; later current-catalog edits rebuild development
documentation only. If every local and OCI copy is deleted, rebuilding the old
tag uses the then-current catalog again.

API Changes includes both the module's primary symbol graph and extension
graphs such as `Module@UIKit.symbols.json`. Add externally extended module names
to `allowedModules` when their generated documentation routes must remain in the
assembled site. Set `symbolGraph.emitExtensionBlocks` to `true` for packages
whose public surface extends types from other modules; this mirrors SwiftPM's
DocC symbol extraction mode and gives DocC a local extension symbol to render.

## SwiftPM plugin

Add VersionedDocC as a direct package dependency:

```swift
.package(
    url: "https://github.com/DocCLab/VersionedDocC.git",
    exact: "0.0.12"
)
```

Build and assemble the site:

```shell
swift package --disable-sandbox \
  --allow-writing-to-package-directory \
  --allow-network-connections all \
  versioned-documentation build \
  --config .vdc.json
```

Serve the result with the configured wildcard redirect:

```shell
swift package --disable-sandbox \
  --allow-writing-to-package-directory \
  --allow-network-connections all \
  versioned-documentation preview \
  --config .vdc.json \
  --port 8766
```

The outer SwiftPM sandbox must be disabled because `build` starts nested
SwiftPM builds (which may access system credential and artifact caches) and
`preview` binds a local server socket. VersionedDocC still uses isolated
checkouts and bounds all cache/output removal to configured child directories.
SwiftPM continues to require the declared write and network grants explicitly.

The standalone executable accepts the same commands:

```shell
swift run VersionedDocC build --package-path /path/to/package
```

## OCI release cache

When `ociCache.repository` is configured, VersionedDocC attempts to restore a
missing release cache before building it. Development documentation such as
`main` stays in the local or GitHub Actions cache by default; set
`includeDevelopment` only when every development commit should become an OCI
artifact.

OCI writes are always explicit:

```shell
swift run VersionedDocC build \
  --package-path /path/to/package \
  --publish-oci-cache
```

Each reference is derived from the version, exact source commit, and build
fingerprint. The artifact contains a deterministic `tar.gz` layer and uses the
media type `application/vnd.openswiftuiproject.versioned-docc.cache.v1`.
VersionedDocC queries before pushing and skips an existing reference; restored
metadata must match the requested source commit and fingerprint before it is
accepted.

Install the `oras` CLI and authenticate before using a remote registry. For
GHCR in GitHub Actions, grant `packages: write` and log in with `GITHUB_TOKEN`.
Use `--no-oci-cache` for an explicitly offline build. Set
`VERSIONED_DOCC_ORAS` when the executable is not on `PATH`.

## Historical URL compatibility

VersionedDocC emits two constant-size compatibility mechanisms:

- `_redirects` gives providers that support wildcard rules, and the local
  preview server, a real HTTP `301`.
- A root `404.html` lets GitHub Pages preserve the requested path and replace
  `/<project>/documentation/...` with
  `/<project>/<default-version>/documentation/...` in the browser.

The Pages fallback is one file for the whole documentation tree; it does not
generate a redirect page for every DocC symbol. GitHub Pages has already
returned `404` before this page runs, so this fallback provides browser and
link compatibility but is not equivalent to an edge/server-side `301` for SEO.

## GitHub Actions

Repositories can call the composite action after checking out full tag
history:

```yaml
- uses: actions/checkout@v7
  with:
    fetch-depth: 0
- uses: oras-project/setup-oras@v1
- run: echo "${{ github.token }}" | oras login ghcr.io --username "${{ github.actor }}" --password-stdin
- uses: DocCLab/VersionedDocC@0.0.12
  with:
    config: .vdc.json
    publish-oci-cache: true
```

OpenSwiftUIProject repositories can alternatively use the reusable workflow:

```yaml
jobs:
  documentation:
    uses: DocCLab/VersionedDocC/.github/workflows/pages.yml@0.0.12
    with:
      config: .vdc.json
      artifact-path: .docs/build/versioned-site
      deploy: true
      publish-oci-cache: true
```

## Artifact contract

Each version cache is self-contained:

```text
.docs/cache/versioned-docc/<version>/
├── metadata.json
├── site/
└── symbols/
    ├── <Module>.symbols.json
    └── <Module>@<ExtendedModule>.symbols.json
```

Adding a release builds only a cache miss. Updating the published version list
only reassembles the selector and API Changes pages; cached Swift and DocC
builds do not rerun. Changing a toolchain, renderer, symbol-graph policy, or
other build input invalidates the affected artifacts through the fingerprint.
VersionedDocC's package version is intentionally separate from that build-cache
revision, so assembly-only changes do not invalidate historical documentation.

The reusable workflow persists this directory with GitHub Actions cache. Its
rolling key restores the previous site cache on a new tag, builds missing
versions, then saves the expanded cache under the new commit key.

With `ociCache` configured, release directories are also stored independently
in the OCI repository. A new release downloads existing references, builds only
the missing version, and publishes only a new immutable artifact. GitHub Actions
cache remains the fast first-level cache for `main` and recent runs.

## Requirements

- Swift 6.0 or newer to build the VersionedDocC executable and Swift package
  symbol graphs. Documentation-only repositories don't need their own Swift
  package.
- Python 3.
- Git and a Swift-DocC toolchain.
- ORAS 1.2 or newer when `ociCache` is enabled.
- macOS for packages whose documentation build requires Apple SDKs.

## License

VersionedDocC is available under the MIT License.
