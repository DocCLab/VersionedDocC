import Foundation

enum LauncherError: Error, CustomStringConvertible {
    case missingResource(String)
    case failedToLaunch(Error)

    var description: String {
        switch self {
        case let .missingResource(name):
            "Missing bundled resource: \(name)"
        case let .failedToLaunch(error):
            "Unable to launch VersionedDocC: \(error)"
        }
    }
}

do {
    guard let scriptURL = Bundle.module.url(
        forResource: "versioned_docc",
        withExtension: "py",
        subdirectory: "Resources"
    ) ?? Bundle.module.url(forResource: "versioned_docc", withExtension: "py") else {
        throw LauncherError.missingResource("versioned_docc.py")
    }

    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
    process.arguments = ["python3", scriptURL.path] + CommandLine.arguments.dropFirst()
    process.currentDirectoryURL = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
    process.standardInput = FileHandle.standardInput
    process.standardOutput = FileHandle.standardOutput
    process.standardError = FileHandle.standardError

    do {
        try process.run()
    } catch {
        throw LauncherError.failedToLaunch(error)
    }
    process.waitUntilExit()
    exit(process.terminationStatus)
} catch {
    FileHandle.standardError.write(Data("error: \(error)\n".utf8))
    exit(1)
}
