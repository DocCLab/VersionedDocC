# Configuring VersionedDocC

Describe which documentation to build, which versions to retain, and where the
assembled site will be hosted.

VersionedDocC discovers `.vdc.json` at the package root. Pass `--config` or the
action's `config` input only when you use a different path.

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

## Publish a standalone catalog

A documentation repository without `Package.swift` can skip symbol graphs:

```json
{
  "$schema": "https://raw.githubusercontent.com/DocCLab/VersionedDocC/0.0.13/Schema/VersionedDocC.schema.json",
  "schemaVersion": 1,
  "documentationOnly": true,
  "projectName": "MyGuide",
  "modulePath": "myguide",
  "catalogPath": "Documentation/MyGuide.docc",
  "hostingBasePath": "/MyGuide",
  "defaultVersion": "main",
  "articleChanges": { "enabled": true },
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
