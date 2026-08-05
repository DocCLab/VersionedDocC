---
name: adopt-versioned-docc
description: Add VersionedDocC to a Swift package or standalone DocC catalog, including .vdc.json configuration, multi-version and multi-platform symbol graphs, GitHub Actions, GitHub Pages, API comparisons, legacy URL compatibility, and optional GHCR OCI caching. Use when a user asks to adopt, configure, migrate to, debug, or validate VersionedDocC documentation publishing.
---

# Adopt VersionedDocC

Integrate VersionedDocC with the smallest configuration that matches the
package's real targets, release history, platforms, and hosting environment.

## Inspect the package

Before editing, inspect:

- `Package.swift`, `Package.resolved`, public targets, and platform declarations.
- Existing `.docc` catalogs and documentation workflows.
- Whether the repository is a Swift package or a standalone DocC catalog.
- Semantic-version tags and the development branch name.
- Public extensions on types from other modules.
- The current Git status; preserve unrelated user changes.

Use repository instructions and avoid an expensive documentation build until
the configuration is coherent. A versioned site requires the development ref
and at least one usable release tag.

## Choose the configuration

Create `.vdc.json` at the package root. Use the same stable VersionedDocC tag in
the schema URL, package dependency, action, and reusable workflow.

Set these fields from the package rather than guessing:

- `projectName`: published site name, normally the repository name.
- `moduleName` and `targetName`: documented Swift module and package target.
- `modulePath`: DocC's lowercase route for the module.
- `catalogPath`: the target's real `.docc` catalog.
- `hostingBasePath`: GitHub Pages project path, normally `/<repository>`.
- `sourceRepository`: canonical HTTPS GitHub repository URL.
- `defaultVersion`: normally `main`.

For a standalone DocC catalog with no `Package.swift`, set
`documentationOnly` to `true`, require an explicit `modulePath` matching the
catalog's generated documentation route, and omit `moduleName`, `targetName`,
and symbol graph configuration. Article changes are disabled by default; set
`articleChanges.enabled` to `true` only when the repository wants authored DocC
articles included in its Changes dashboard. Use explicit `versions` when the
upstream tags aren't semantic versions.

Prefer `releasePolicy` for normal packages. `latest: 2` publishes the newest
patch from each of the two newest distinct `major.minor` release lines; it does
not publish two patches from one release line. Use `pinned` only for an
additional stable baseline. Use explicit `versions` only when exact snapshots
must never change.

Set `historicalCatalogFallback` to `current` only when selected historical tags
predate the DocC catalog. The fallback is used on the first cache miss and the
resulting immutable release cache is reused afterward.

## Configure symbol graphs

Keep `skipProtocolImplementations` enabled so protocol-extension members are
not repeated for every conforming type.

For an Apple UI package, normally use iOS as `defaultPlatform` and add macOS as
a secondary platform. Match the package's supported platforms and SDKs; do not
add platforms the package cannot build.

If the package adds public extensions to types from UIKit, AppKit, QuartzCore,
SwiftUI, or another external module:

- Set `symbolGraph.emitExtensionBlocks` to `true`.
- Add only the externally extended modules whose routes must remain to
  `allowedModules`, together with the package module.

## Add CI publishing

Add or adapt a documentation workflow that:

1. Runs for the development branch, semantic-version tags, and manual dispatch.
2. Checks out the development branch with `fetch-depth: 0`, so the current
   configuration and complete tag history are available on tag-triggered runs.
3. Restores `.docs/cache/versioned-docc` with a rolling GitHub Actions cache.
4. Selects the required Xcode/toolchain and DocC renderer before building.
5. Invokes `DocCLab/VersionedDocC@<stable-tag>` with `.vdc.json`.
6. Uploads `.docs/build/versioned-site/<projectName>` as the Pages artifact and
   deploys it with `actions/deploy-pages`.

For OCI caching, grant `packages: write`, install ORAS, log in to GHCR with the
workflow token, configure a lowercase repository such as
`ghcr.io/<owner>/<repository>-docc-cache`, and pass
`publish-oci-cache: true`. Keep release artifacts immutable; a new release
should restore existing releases and publish only its own cache miss.

## Validate

Run the narrowest useful checks first:

1. Validate `.vdc.json` syntax and inspect the diff.
2. Confirm the selected tags represent distinct `major.minor` release lines.
3. Build locally without OCI publishing, then run it again and confirm cache
   hits for every unchanged version.
4. Inspect `<site-root>/versions.json` and, for Swift packages, the
   `main/changes/` comparisons.
5. Preview with VersionedDocC so legacy URL redirects are exercised.
6. After CI, verify the Pages URL and confirm missing historical OCI caches were
   built once and published.

Report the selected versions, cache hits and misses, comparison counts, preview
URL, and any warnings. Do not claim success from a green workflow alone; inspect
the generated site and build log.

For the canonical configuration fields and commands, consult the VersionedDocC
README and `Schema/VersionedDocC.schema.json` from the same release as the
action being adopted.
