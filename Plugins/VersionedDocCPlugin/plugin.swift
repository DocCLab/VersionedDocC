import Foundation
import PackagePlugin

enum PluginError: Error, CustomStringConvertible {
    case toolFailed(Int32)

    var description: String {
        switch self {
        case let .toolFailed(status):
            "versioned-docc exited with status \(status)"
        }
    }
}

@main
struct VersionedDocCPlugin: CommandPlugin {
    func performCommand(context: PluginContext, arguments: [String]) async throws {
        let tool = try context.tool(named: "versioned-docc")
        let process = Process()
        process.executableURL = tool.url
        process.arguments = arguments
        process.currentDirectoryURL = context.package.directoryURL
        process.standardInput = FileHandle.standardInput
        process.standardOutput = FileHandle.standardOutput
        process.standardError = FileHandle.standardError
        try process.run()
        process.waitUntilExit()
        guard process.terminationStatus == 0 else {
            throw PluginError.toolFailed(process.terminationStatus)
        }
    }
}
