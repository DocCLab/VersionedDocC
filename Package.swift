// swift-tools-version: 6.0

import PackageDescription

let package = Package(
    name: "VersionedDocC",
    platforms: [
        .macOS(.v13),
    ],
    products: [
        .executable(name: "VersionedDocC", targets: ["VersionedDocC"]),
        .plugin(name: "VersionedDocCPlugin", targets: ["VersionedDocCPlugin"]),
    ],
    targets: [
        .executableTarget(
            name: "VersionedDocC",
            resources: [.copy("Resources")]
        ),
        .plugin(
            name: "VersionedDocCPlugin",
            capability: .command(
                intent: .custom(
                    verb: "versioned-documentation",
                    description: "Build and assemble versioned DocC documentation"
                ),
                permissions: [
                    .writeToPackageDirectory(
                        reason: "Write versioned documentation artifacts and caches"
                    ),
                ]
            ),
            dependencies: [.target(name: "VersionedDocC")]
        ),
    ]
)
