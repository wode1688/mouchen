import Foundation

struct SharedCapture: Codable, Identifiable, Sendable {
    let id: UUID
    let text: String
    let sourceApplication: String?
    let capturedAt: Date

    init(text: String, sourceApplication: String? = nil) {
        self.id = UUID()
        self.text = String(text.prefix(12_000))
        self.sourceApplication = sourceApplication
        self.capturedAt = Date()
    }
}
enum SharedCaptureStore {
    static let suiteName = "group.com.mouchen.ios"
    private static let filename = "pending-shared-captures-v2.json"
    private static let legacyKey = "pending-shared-captures-v1"

    static func append(_ capture: SharedCapture, root: URL? = nil) {
        let url = storageURL(root: root)
        migrateLegacyIfNeeded(to: url)
        coordinateWriting(url) {
            var current = load(url)
            current.append(capture)
            if current.count > 100 { current.removeFirst(current.count - 100) }
            try? protectedWrite(try JSONEncoder().encode(current), to: url)
        }
    }

    static func takeAll(root: URL? = nil) -> [SharedCapture] {
        let url = storageURL(root: root)
        migrateLegacyIfNeeded(to: url)
        var current: [SharedCapture] = []
        coordinateWriting(url) {
            current = load(url)
            guard !current.isEmpty else { return }
            do {
                try FileManager.default.removeItem(at: url)
            } catch {
                // Fail closed: returning no captures prevents the same private
                // payload from being copied while it remains on disk.
                current = []
            }
        }
        return current
    }

    static func removeAll(root: URL? = nil) throws {
        let url = storageURL(root: root)
        UserDefaults(suiteName: suiteName)?.removeObject(forKey: legacyKey)
        var removalError: Error?
        coordinateWriting(url) {
            guard FileManager.default.fileExists(atPath: url.path) else { return }
            do { try FileManager.default.removeItem(at: url) }
            catch { removalError = error }
        }
        if let removalError { throw removalError }
    }

    static func storageURL(root: URL? = nil) -> URL {
        let manager = FileManager.default
        let directory = root
            ?? manager.containerURL(forSecurityApplicationGroupIdentifier: suiteName)
            ?? manager.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        try? manager.createDirectory(
            at: directory,
            withIntermediateDirectories: true,
            attributes: [.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication]
        )
        return directory.appendingPathComponent(filename, isDirectory: false)
    }

    static func load(_ url: URL) -> [SharedCapture] {
        guard let data = try? Data(contentsOf: url) else { return [] }
        return (try? JSONDecoder().decode([SharedCapture].self, from: data)) ?? []
    }

    private static func protectedWrite(_ data: Data, to url: URL) throws {
        try data.write(to: url, options: [.atomic, .completeFileProtection])
        try FileManager.default.setAttributes(
            [.protectionKey: FileProtectionType.complete],
            ofItemAtPath: url.path
        )
    }

    private static func migrateLegacyIfNeeded(to url: URL) {
        guard let defaults = UserDefaults(suiteName: suiteName),
              let data = defaults.data(forKey: legacyKey) else { return }
        // Remove the preferences copy first. A crash may require the owner to
        // reshare the item, but it cannot leave the private body duplicated in
        // an unprotected preferences database.
        defaults.removeObject(forKey: legacyKey)
        guard let legacy = try? JSONDecoder().decode([SharedCapture].self, from: data),
              !legacy.isEmpty else { return }
        coordinateWriting(url) {
            var current = load(url)
            current.append(contentsOf: legacy)
            if current.count > 100 { current.removeFirst(current.count - 100) }
            try? protectedWrite(try JSONEncoder().encode(current), to: url)
        }
    }

    private static func coordinateWriting(_ url: URL, operation: () -> Void) {
        let coordinator = NSFileCoordinator(filePresenter: nil)
        var coordinationError: NSError?
        coordinator.coordinate(writingItemAt: url, options: [], error: &coordinationError) { _ in
            operation()
        }
    }
}
