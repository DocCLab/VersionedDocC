# Adopting VersionedDocC

Add version-aware DocC publishing to a Swift package in a few focused steps.

## Before you begin

Your repository needs Swift 6 or newer, Python 3, Git, and a Swift toolchain that
contains DocC. On GitHub Actions, use a macOS runner when the package builds for
Apple platforms or needs Apple SDKs.

VersionedDocC expects the repository's full tag history. Its publishing workflow
checks out with `fetch-depth: 0` for this reason.

## 1. Add the package dependency

Add VersionedDocC as a direct dependency in `Package.swift`:

```swift
dependencies: [
    .package(
        url: "https://github.com/DocCLab/VersionedDocC.git",
        exact: "0.0.18"
    ),
]
```

The package exposes the `versioned-documentation` command plugin. It doesn't
need to be added to one of your package targets.

> Tip: You can also copy the repository's
> [`adopt-versioned-docc`](https://github.com/DocCLab/VersionedDocC/tree/main/skills/adopt-versioned-docc)
> skill into Codex or another Agent Skills-compatible tool and ask it to inspect
> your package, choose platforms, and create the configuration and workflow.

## 2. Create `.vdc.json`

Start with this configuration at the repository root, then replace the example
names, paths, and repository URL:

```json
{
  "$schema": "https://raw.githubusercontent.com/DocCLab/VersionedDocC/0.0.18/Schema/VersionedDocC.schema.json",
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
  "symbolGraph": {
    "minimumAccessLevel": "public",
    "skipProtocolImplementations": true
  }
}
```

For a GitHub project site, `hostingBasePath` normally matches the repository name
exactly, including capitalization. `modulePath` is the lowercase route DocC uses
after `/documentation/`; verify it against an existing local DocC build if the
module has a custom display name. Set `hostingBasePath` to `/` when a custom
domain serves the site at its root.

See <doc:Configuring-VersionedDocC> for multi-platform builds, fixed version
lists, and standalone catalogs.

## 3. Test locally

Build and assemble all configured versions:

```shell
swift package --disable-sandbox \
  --allow-writing-to-package-directory \
  --allow-network-connections all \
  versioned-documentation build \
  --config .vdc.json
```

Then serve the assembled site with its production base path:

```shell
swift package --disable-sandbox \
  --allow-writing-to-package-directory \
  --allow-network-connections all \
  versioned-documentation preview \
  --config .vdc.json \
  --port 8766
```

Open the URL printed by `preview`. Check at least the development version, a
release, the version selector, source links, and the Changes page.

## 4. Publish

Add the workflow from <doc:Publishing-to-GitHub-Pages>, select **GitHub Actions**
as the Pages source in the repository settings, and push to the default branch.

The first run builds every selected version. Later runs restore immutable version
caches and rebuild only cache misses, then reassemble the site with the current
selector and Changes pages.
