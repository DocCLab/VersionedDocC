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
- Exact historical local dependencies from each tag's `Package.resolved`.
- A single provider-style wildcard redirect from legacy documentation URLs to
  the configured default version.
- A SwiftPM command plugin, standalone executable, composite GitHub Action,
  and reusable Pages workflow.

## Configuration

Add `VersionedDocC.json` to the package root:

```json
{
  "$schema": "https://raw.githubusercontent.com/OpenSwiftUIProject/VersionedDocC/0.0.1/Schema/VersionedDocC.schema.json",
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
    "development": { "name": "main", "ref": "HEAD" }
  },
  "sourceRepository": "https://github.com/Example/ExampleKit",
  "symbolGraph": {
    "minimumAccessLevel": "public",
    "skipProtocolImplementations": true
  }
}
```

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

## SwiftPM plugin

Add VersionedDocC as a direct package dependency:

```swift
.package(
    url: "https://github.com/OpenSwiftUIProject/VersionedDocC.git",
    exact: "0.0.1"
)
```

Build and assemble the site:

```shell
swift package --allow-writing-to-package-directory \
  --allow-network-connections all \
  versioned-documentation build \
  --config VersionedDocC.json
```

Serve the result with the configured wildcard redirect:

```shell
swift package --allow-writing-to-package-directory \
  versioned-documentation preview \
  --config VersionedDocC.json \
  --port 8766
```

The standalone executable accepts the same commands:

```shell
swift run VersionedDocC build --package-path /path/to/package
```

## GitHub Actions

Repositories can call the composite action after checking out full tag
history:

```yaml
- uses: actions/checkout@v7
  with:
    fetch-depth: 0
- uses: OpenSwiftUIProject/VersionedDocC@0.0.1
  with:
    config: VersionedDocC.json
```

OpenSwiftUIProject repositories can alternatively use the reusable workflow:

```yaml
jobs:
  documentation:
    uses: OpenSwiftUIProject/VersionedDocC/.github/workflows/pages.yml@0.0.1
    with:
      config: VersionedDocC.json
      artifact-path: .docs/build/versioned-site
      deploy: true
```

## Artifact contract

Each version cache is self-contained:

```text
.docs/cache/versioned-docc/<version>/
├── metadata.json
├── site/
└── symbols/<Module>.symbols.json
```

Adding a release builds only a cache miss. Updating the published version list
only reassembles the selector and API Changes pages; cached Swift and DocC
builds do not rerun. Changing a toolchain, renderer, symbol-graph policy, or
other build input invalidates the affected artifacts through the fingerprint.

## Requirements

- Swift 6.0 or newer.
- Python 3.
- Git and a Swift-DocC toolchain.
- macOS for packages whose documentation build requires Apple SDKs.

## License

VersionedDocC is available under the MIT License.
