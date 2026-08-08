# VersionedDocC

Publish every important version of your Swift-DocC documentation at a stable URL.

VersionedDocC builds documentation from your development branch and release tags,
caches each result as an immutable artifact, and assembles everything into one
GitHub Pages-ready site. Readers get a version selector, a visible build date,
and adjacent-version Changes pages without replacing Swift-DocC or its renderer.

@Metadata {
    @TechnologyRoot
}

## Start publishing

Add one configuration file and one GitHub Actions workflow. VersionedDocC handles
historical checkouts, symbol graphs, DocC conversion, cache reuse, and Pages
assembly for you.

- <doc:Adopting-VersionedDocC>
- <doc:Configuring-VersionedDocC>
- <doc:Publishing-to-GitHub-Pages>

## What you get

- Stable URLs such as `/MyPackage/1.4/documentation/mypackage/`.
- A version menu shared by every generated DocC page.
- Public API changes between adjacent releases.
- Optional article changes for authored documentation.
- Incremental local, GitHub Actions, and optional OCI caches.
- Support for both Swift packages and standalone DocC catalogs.

## Use VersionedDocC

- [VersionedDocC on GitHub](https://github.com/DocCLab/VersionedDocC)
- [Install the adoption skill](https://github.com/DocCLab/VersionedDocC/tree/main/skills/adopt-versioned-docc)
- [Browse the JSON schema](https://github.com/DocCLab/VersionedDocC/blob/main/Schema/VersionedDocC.schema.json)
