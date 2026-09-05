import Social
import UniformTypeIdentifiers

final class ShareViewController: SLComposeServiceViewController {
    override func viewDidLoad() {
        super.viewDidLoad()
        title = SharedLocaleStore.text("发送到AI替身", "Send to My AI Twin")
    }

    override func isContentValid() -> Bool {
        if !contentText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { return true }
        guard let items = extensionContext?.inputItems as? [NSExtensionItem] else { return false }
        return items
            .flatMap { $0.attachments ?? [] }
            .contains { provider in
                provider.hasItemConformingToTypeIdentifier(UTType.url.identifier)
                    || provider.hasItemConformingToTypeIdentifier(UTType.plainText.identifier)
            }
    }
    override func configurationItems() -> [Any]! { [] }

    override func didSelectPost() {
        let initial = contentText.trimmingCharacters(in: .whitespacesAndNewlines)
        Task {
            let extracted = await extractSharedText()
            let combined = [initial, extracted].filter { !$0.isEmpty }.joined(separator: "\n")
            if !combined.isEmpty {
                SharedCaptureStore.append(SharedCapture(text: combined, sourceApplication: nil))
            }
            extensionContext?.completeRequest(returningItems: [], completionHandler: nil)
        }
    }

    private func extractSharedText() async -> String {
        guard let items = extensionContext?.inputItems as? [NSExtensionItem] else { return "" }
        var values: [String] = []
        for provider in items.flatMap({ $0.attachments ?? [] }) {
            if provider.hasItemConformingToTypeIdentifier(UTType.url.identifier),
               let item = try? await provider.loadItem(forTypeIdentifier: UTType.url.identifier),
               let url = item as? URL {
                values.append(url.absoluteString)
            } else if provider.hasItemConformingToTypeIdentifier(UTType.plainText.identifier),
                      let item = try? await provider.loadItem(forTypeIdentifier: UTType.plainText.identifier),
                      let text = item as? String {
                values.append(text)
            }
        }
        return values.joined(separator: "\n")
    }

}
