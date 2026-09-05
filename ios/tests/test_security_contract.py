from pathlib import Path


IOS = Path(__file__).resolve().parents[1]


def source(relative: str) -> str:
    return (IOS / relative).read_text(encoding="utf-8")


def test_authenticated_client_does_not_send_user_identity_header() -> None:
    api = source("Mouchen/APIClient.swift")
    assert 'forHTTPHeaderField: "X-User-Id"' not in api
    assert "OriginBoundSessionDelegate" in api
    assert "sameOrigin(origin, responseURL)" in api
    assert "isHTTPSDowngrade(origin, responseURL)" in api


def test_session_epoch_guards_network_and_queue_writes() -> None:
    model = source("Mouchen/AppModel.swift")
    queue = source("Mouchen/EventQueue.swift")
    assert "private var sessionEpoch: UInt64" in model
    assert "syncTask?.cancel()" in model
    assert "authenticationTask?.cancel()" in model
    assert "guard isCurrent(snapshot) else { return }" in model
    assert "queue.remove(item.id, namespace: snapshot.attentionNamespace)" in model
    assert "switchNamespace(from: existingNamespace, to: checkedNamespace)" in model
    assert "sealSignedOut(namespace: namespace, epoch: operationEpoch)" in model
    assert "Local event queue belongs to a different account" in queue


def test_sensitive_full_text_is_not_stored_in_user_defaults() -> None:
    captures = source("Shared/SharedCaptureStore.swift")
    attention = source("Mouchen/NotificationService.swift")
    attention_store = attention.split("actor AdviceNotificationService", maxsplit=1)[0]
    assert "defaults.set" not in captures
    assert "removeObject(forKey: legacyKey)" in captures
    assert "Data(contentsOf:" in captures
    assert ".completeFileProtection" in captures
    assert "defaults.set" not in attention_store
    assert ".completeFileProtection" in attention_store
    assert "SHA256.hash" in attention_store


def test_swift_unit_tests_cover_origin_and_file_namespacing() -> None:
    tests = source("MouchenTests/MouchenTests.swift")
    assert "testOriginComparisonRejectsCrossHostPortAndDowngrade" in tests
    assert "testAttentionStateIsTenantNamespaced" in tests
    assert "testSharedCaptureUsesProtectedFileInsteadOfPreferences" in tests


def test_account_locale_uses_authenticated_server_contract() -> None:
    api = source("Mouchen/APIClient.swift")
    model = source("Mouchen/AppModel.swift")
    models = source("Mouchen/Models.swift")
    assert 'path: "/v1/account/preferences"' in api
    assert 'method: "PUT"' in api
    assert "let locale: String?" in models
    assert "async let remotePreferences = client.accountPreferences()" in model
    assert "try applyRemoteLocale(pulledPreferences.locale, snapshot: snapshot)" in model
    assert "guard isCurrent(snapshot)" in model
    assert "updated.locale = serverLocale.rawValue" in model
    assert "try keychain.save(updated)" in model


def test_account_locale_put_and_get_have_a_deterministic_ordering_contract() -> None:
    model = source("Mouchen/AppModel.swift")
    fence = source("Mouchen/LocalePreferenceFence.swift")
    tests = source("MouchenTests/MouchenTests.swift")
    assert "private var localePreferenceFence = LocalePreferenceFence()" in model
    assert "private var pendingLocaleSelection = LatestLocaleSelection()" in model
    assert "let localeReadToken = localePreferenceFence.beginRead()" in model
    assert "guard localePreferenceFence.permits(localeReadToken) else { return }" in model
    assert "localePreferenceFence.finishMutation(mutationToken)" in model
    assert "activeMutationGeneration == nil" in fence
    assert "generation &+= 1" in fence
    assert "testLocalePreferenceFenceRejectsGetThatStartedBeforePut" in tests
    assert "testLocalePreferenceFenceBlocksGetUntilPutFinishes" in tests
    assert "testRapidLocaleSelectionsKeepOnlyTheNewestValue" in tests


def test_language_switch_does_not_translate_owner_evidence() -> None:
    root = source("Mouchen/RootView.swift")
    localization = source("Mouchen/Localization.swift")
    notification = source("Mouchen/NotificationService.swift")
    assert 'case zhCN = "zh-CN"' in localization
    assert 'case enUS = "en-US"' in localization
    assert "model.updateLocale(value)" in root
    assert "item.goalQuote" in root
    assert "Goal quotes and evidence stay verbatim" in root
    assert "deliver(_ claim: PendingAdviceClaim, locale: AppLocale)" in notification
    assert '"First step"' in notification
    shared_locale = source("Shared/SharedLocaleStore.swift")
    assert 'private static let key = "account-locale-v1"' in shared_locale
    assert "credential" in shared_locale
    assert "capture body" in shared_locale
    assert "SharedLocaleStore.text" in source("MouchenShare/ShareViewController.swift")


def test_registration_sends_selected_locale_but_login_uses_server_locale() -> None:
    api = source("Mouchen/APIClient.swift")
    model = source("Mouchen/AppModel.swift")
    assert "locale: locale.rawValue" in api
    assert "locale: nil" in api
    assert "response.session.locale" in model
    assert "checked.locale = session.locale.map" in model


def test_info_plist_privacy_copy_exists_in_both_languages() -> None:
    zh = source("Mouchen/zh-Hans.lproj/InfoPlist.strings")
    en = source("Mouchen/en.lproj/InfoPlist.strings")
    share_en = source("MouchenShare/en.lproj/InfoPlist.strings")
    assert "NSCalendarsFullAccessUsageDescription" in zh
    assert "NSCalendarsFullAccessUsageDescription" in en
    assert '"CFBundleDisplayName" = "My AI Twin";' in en
    assert '"CFBundleDisplayName" = "Send to My AI Twin";' in share_en
