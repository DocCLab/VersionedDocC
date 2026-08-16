# Publishing to GitHub Pages

Use the reusable workflow to build, cache, upload, and optionally deploy your
versioned documentation.

## Add the workflow

Create `.github/workflows/documentation.yml`:

```yaml
name: Documentation

on:
  push:
    branches: [main]
    tags: ["*"]
  workflow_dispatch:

permissions:
  contents: read
  packages: write
  pages: write
  id-token: write

concurrency:
  group: pages
  cancel-in-progress: false

jobs:
  documentation:
    uses: DocCLab/VersionedDocC/.github/workflows/pages.yml@0.0.17
    with:
      config: .vdc.json
      artifact-path: .docs/build/versioned-site/ExampleKit
      deploy: true
```

Replace `ExampleKit` in `artifact-path` with the final component of your
`hostingBasePath`. Upload that directory itself, not its parent: GitHub already
serves a project Pages artifact below the repository's path.

The tag trigger republishes the selector when a new release appears. A tag push
doesn't need to run on every tag in your repository; narrow the pattern if only
some tags represent releases.

## Enable Pages

In the GitHub repository, open **Settings > Pages** and select **GitHub Actions**
as the source. The workflow's deploy job writes to the `github-pages`
environment and reports the final URL.

The workflow needs `contents: read`, `pages: write`, and `id-token: write`.
The reusable workflow also logs in to GHCR so it can restore or publish an OCI
cache when configured, so its caller grants `packages: write`. A custom workflow
that uses only the composite action can omit package permission when OCI caching
isn't configured.

## Use the composite action directly

Use the action when you need custom runners, Xcode selection, extra validation,
or a different deployment provider:

```yaml
- uses: actions/checkout@v7
  with:
    fetch-depth: 0
- uses: DocCLab/VersionedDocC@0.0.17
  with:
    config: .vdc.json
- uses: actions/upload-pages-artifact@v5
  with:
    path: .docs/build/versioned-site/ExampleKit
```

Always fetch complete history so VersionedDocC can resolve release tags and
exact historical dependency revisions.

## Publish immutable caches to GHCR

For large documentation builds, add an `ociCache` repository:

```json
"ociCache": {
  "repository": "ghcr.io/example/examplekit-docc-cache"
}
```

Then pass `publish-oci-cache: true` to the action or reusable workflow. OCI
writes are explicit: VersionedDocC checks for an existing reference, publishes
only missing immutable release artifacts, and leaves development caches local
unless `includeDevelopment` is enabled.

## Verify the deployment

After the first successful run, check:

1. The repository Pages URL redirects to the default version's documentation.
2. The version selector opens both `main` and at least one release.
3. The Changes link compares adjacent versions.
4. A legacy unversioned `/documentation/...` URL reaches the default version.
5. A second workflow run restores caches instead of rebuilding every version.
