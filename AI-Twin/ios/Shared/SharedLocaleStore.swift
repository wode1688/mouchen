import Foundation

/// The selected language is not private user content, so it may be shared with
/// the extension through the App Group preferences store. No credential,
/// capture body, or tenant identifier is stored here.
enum SharedLocaleStore {
    private static let key = "account-locale-v1"

    static var rawValue: String? {
        UserDefaults(suiteName: SharedCaptureStore.suiteName)?.string(forKey: key)
    }

    static var isEnglish: Bool {
        rawValue?.lowercased().hasPrefix("en") == true
    }

    static func save(_ rawValue: String) {
        UserDefaults(suiteName: SharedCaptureStore.suiteName)?.set(rawValue, forKey: key)
    }

    static func text(_ zh: String, _ en: String) -> String {
        isEnglish ? en : zh
    }
}
