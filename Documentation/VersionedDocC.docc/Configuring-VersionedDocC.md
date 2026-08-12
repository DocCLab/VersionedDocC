# Configuring VersionedDocC

Describe which documentation to build, which versions to retain, and where the
assembled site will be hosted.

VersionedDocC discovers `.vdc.json` at the package root. Pass `--config` or the
action's `config` input only when you use a different path.

## Configure site actions

With `sourceRepository` configured, Edit and Star links are enabled by default.
Use `siteUI` to make each choice explicit:

```json
"siteUI": {
  "showEdit": true,
  "showStar": true,
  "showPoweredBy": true
}
```

**Edit this page** uses the current documentation version's `sourceRef`.
Authored articles and documentation extensions open their Markdown file in the
GitHub editor; other symbol pages open the declaration source reported by DocC.
Generated pages without reliable source metadata don't show an edit link.

**Star on GitHub** appears in the header without loading a star counter or a
third-party script. **Powered by VersionedDocC** appears at the bottom of DocC
pages and the Changes dashboard. Set any field to `false` to hide that element.
VersionedDocC appends its content after an existing custom DocC footer.

## Choose versions

Use `releasePolicy` when releases follow semantic version tags:

```json
"releasePolicy": {
  "latest": 2,
  "latestStrategy": "majorMinor",
  "development": { "name": "main", "ref": "HEAD" },
  "pinned": ["1.0.0"]
}
```

The available strategies are:

- `majorMinor`: keep the highest patch from each of the newest release series.
- `semanticVersion`: keep the highest individual semantic-version tags.
- `tagDate`: keep the most recently created tags.

Use `versions` instead when the published list must be exact:

```json
"versions": [
  { "name": "main", "ref": "HEAD", "sourceRef": "main" },
  { "name": "2.1", "ref": "2.1.4" },
  { "name": "2.0", "ref": "2.0.7" }
]
```

Each version name becomes part of its public URL. Changing the displayed name
therefore changes URLs even when `ref` still points to the same commit.

## Build multiple platforms

Build one symbol graph per platform when APIs or availability differ between
SDKs. VersionedDocC passes all graphs to one DocC conversion:

```json
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
}
```

Choose the platform that represents most consumers as `defaultPlatform`.
VersionedDocC uses its declaration as the primary representation when the same
symbol appears in more than one graph.

## Merge modules built by existing CI jobs

Use `additionalModules` for products whose symbol graphs must be emitted on a
different runner, such as macOS, Linux, iOS, and Windows backends:

```json
"additionalModules": [
  {
    "moduleName": "AppKitBackend",
    "symbolGraphPath": ".docs/symbol-graphs/{version}/AppKitBackend"
  },
  {
    "moduleName": "UIKitBackend",
    "symbolGraphPath": ".docs/symbol-graphs/{version}/UIKitBackend"
  }
]
```

The path may point to one `.symbols.json` file or a directory. It supports the
`{version}`, `{ref}`, `{commit}`, and `{module}` placeholders. Download and
extract the artifacts before running VersionedDocC; the tool validates the
module, remaps CI checkout locations, converts each module, and merges the
archives into one versioned site.

Set `catalogPath` on a module when it has authored documentation. Without one,
VersionedDocC supplies an empty catalog. Set `versions` to an array of published
version names only when the module is intentionally unavailable elsewhere.
Otherwise an external graph input is required for every version. All imported
module graphs participate in adjacent-version API Changes.

## Publish a standalone catalog

A documentation repository without `Package.swift` can skip symbol graphs:

```json
{
  "$schema": "https://raw.githubusercontent.com/DocCLab/VersionedDocC/0.0.16/Schema/VersionedDocC.schema.json",
  "schemaVersion": 1,
  "documentationOnly": true,
  "projectName": "MyGuide",
  "modulePath": "myguide",
  "catalogPath": "Documentation/MyGuide.docc",
  "hostingBasePath": "/MyGuide",
  "defaultVersion": "main",
  "articleChanges": { "enabled": true },
  "siteUI": {
    "showEdit": true,
    "showStar": true,
    "showPoweredBy": true
  },
  "releasePolicy": {
    "latest": 2,
    "development": { "name": "main", "ref": "HEAD" }
  },
  "sourceRepository": "https://github.com/Example/MyGuide"
}
```

`articleChanges` is opt-in. It compares stable rendered article content while
ignoring source-service metadata and linked-page metadata.

If the catalog was added after older releases, set
`"historicalCatalogFallback": "current"` to build those releases with today's
catalog. VersionedDocC records the fallback commit in cache metadata.

## Keep stable URLs

VersionedDocC emits a wildcard `_redirects` file and a GitHub Pages `404.html`
fallback. Together they preserve old unversioned links such as
`/MyGuide/documentation/myguide/` by sending readers to the configured default
version.

Treat these fields as public URL contracts:

- `hostingBasePath`
- each version's `name`
- `modulePath`
- `defaultVersion`
