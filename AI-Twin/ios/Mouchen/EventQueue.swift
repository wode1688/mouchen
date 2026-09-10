import Foundation

struct QueuedEvent: Codable, Identifiable, Sendable {
    let id: UUID
    let event: EventPayload
    let queuedAt: Date

    init(event: EventPayload) {
        self.id = UUID()
        self.event = event
        self.queuedAt = Date()
    }
}
actor EventQueue {
    private struct Envelope: Codable {
        var namespace: String?
        var events: [QueuedEvent]
    }

    private let fileURL: URL
    private var events: [QueuedEvent]
    private var namespace: String?

    init(fileURL providedFileURL: URL? = nil) {
        let manager = FileManager.default
        let root = manager.containerURL(forSecurityApplicationGroupIdentifier: SharedCaptureStore.suiteName)
            ?? manager.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        try? manager.createDirectory(at: root, withIntermediateDirectories: true)
        fileURL = providedFileURL ?? root.appendingPathComponent("mouchen-ios-event-queue.json")
        if let data = try? Data(contentsOf: fileURL),
           let decoded = try? JSONDecoder().decode(Envelope.self, from: data) {
            namespace = decoded.namespace
            events = decoded.events
        } else if let data = try? Data(contentsOf: fileURL),
                  let legacy = try? JSONDecoder().decode([QueuedEvent].self, from: data) {
            namespace = nil
            events = legacy
        } else {
            namespace = nil
            events = []
        }
    }

    @discardableResult
    func enqueue(_ event: EventPayload, namespace expected: String) throws -> UUID {
        try requireNamespace(expected)
        let queued = QueuedEvent(event: event)
        events.append(queued)
        if events.count > 5_000 { events.removeFirst(events.count - 5_000) }
        try persist()
        return queued.id
    }

    func first(limit: Int, namespace expected: String) throws -> [QueuedEvent] {
        try requireNamespace(expected)
        return Array(events.prefix(max(1, limit)))
    }

    func count(namespace expected: String) throws -> Int {
        try requireNamespace(expected)
        return events.count
    }

    func remove(_ id: UUID, namespace expected: String) throws {
        try requireNamespace(expected)
        events.removeAll { $0.id == id }
        try persist()
    }

    func removeAll(namespace expected: String, unbind: Bool = false) throws {
        try requireNamespace(expected)
        events.removeAll()
        if unbind { namespace = nil }
        try persist()
    }

    func switchNamespace(from expected: String, to replacement: String) throws {
        try requireNamespace(expected)
        events.removeAll()
        namespace = replacement
        try persist()
    }

    func sealSignedOut(namespace expected: String, epoch: UInt64) throws {
        try requireNamespace(expected)
        events.removeAll()
        namespace = "signed-out-\(epoch)"
        try persist()
    }

    func activateNamespace(_ expected: String) throws {
        if namespace == nil || namespace?.hasPrefix("signed-out-") == true {
            guard events.isEmpty else {
                throw MouchenError.configuration("Signed-out queue unexpectedly contains events")
            }
            namespace = expected
            try persist()
            return
        }
        try requireNamespace(expected)
    }

    private func requireNamespace(_ expected: String) throws {
        if namespace == nil {
            namespace = expected
            try persist()
        }
        guard namespace == expected else {
            throw MouchenError.configuration("Local event queue belongs to a different account")
        }
    }

    private func persist() throws {
        let data = try JSONEncoder().encode(Envelope(namespace: namespace, events: events))
        try data.write(to: fileURL, options: [.atomic, .completeFileProtection])
    }
}
