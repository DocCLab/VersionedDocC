# VersionedDocC

VersionedDocC builds Swift-DocC documentation for a development branch and
published releases, stores each version as an immutable artifact, assembles a
version-aware static website, and generates public API comparisons from Swift
symbol graphs.

VersionedDocC is an orchestration layer around the Swift toolchain's `swift`
and `docc` executables. It does not fork Swift-DocC or Swift-DocC Render.

## Features

- Stable `/<project>/<version>/documentation/...` URLs.
- A custom DocC header with version selection and UTC build date.
- Adjacent-version API Changes generated from real public symbol graphs.
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

## Examples

- **OpenSwiftUI:** [GitHub repository](https://github.com/OpenSwiftUIProject/OpenSwiftUI)
  · [Live versioned documentation](https://openswiftuiproject.github.io/OpenSwiftUI/main/)
- **ScreenShieldKit:** [GitHub repository](https://github.com/Kyle-Ye/ScreenShieldKit)
  · [Live versioned documentation](https://kyle-ye.github.io/ScreenShieldKit/main/)

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
  "$schema": "https://raw.githubusercontent.com/OpenSwiftUIProject/VersionedDocC/0.0.9/Schema/VersionedDocC.schema.json",
  "schemaVersion": 1,
  "projectName": "ExampleKit",
  "moduleName": "ExampleKit",
  "modulePath": "examplekit",
  "targetName": "ExampleKit",
  "catalogPath": "Sources/ExampleKit/ExampleKit.docc",
  "hostingBasePath": "/ExampleKit",
  "defaultVersion": "main",
  "releasePolicy": {
    "latest": 1,
    "pinned": ["0.19.0"],
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

The CLI, SwiftPM plugin, composite action, and reusable workflow discover this
file automatically. Use `--config` or the `config` action input only for a
nonstandard path.

`latest` follows the newest distinct `major.minor` release series automatically,
using only the highest patch tag from each series. For example, `latest: 2`
selects `0.20.1` and `0.19.3` instead of publishing `0.20.1` alongside `0.20.0`.
`pinned` adds stable comparison baselines without duplicating a release series
already selected by `latest`. An explicit `versions` array remains exact and is
not grouped by release series. The example currently publishes `main`, the
newest release series, and `0.19.0`.
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
    url: "https://github.com/OpenSwiftUIProject/VersionedDocC.git",
    exact: "0.0.9"
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
- uses: OpenSwiftUIProject/VersionedDocC@0.0.9
  with:
    config: .vdc.json
    publish-oci-cache: true
```

OpenSwiftUIProject repositories can alternatively use the reusable workflow:

```yaml
jobs:
  documentation:
    uses: OpenSwiftUIProject/VersionedDocC/.github/workflows/pages.yml@0.0.9
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

- Swift 6.0 or newer.
- Python 3.
- Git and a Swift-DocC toolchain.
- ORAS 1.2 or newer when `ociCache` is enabled.
- macOS for packages whose documentation build requires Apple SDKs.

## License

VersionedDocC is available under the MIT License.
