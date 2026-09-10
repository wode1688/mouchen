import BackgroundTasks
import SwiftUI
import UIKit
import UserNotifications

@main
struct MouchenApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var model = AppModel()
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(model)
                .task {
                    BackgroundSyncRegistry.shared.handler = { await model.sync() }
                    BackgroundSyncRegistry.schedule()
                }
        }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active { Task { await model.sync() } }
        }
    }
}
final class AppDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        UNUserNotificationCenter.current().delegate = self
        BGTaskScheduler.shared.register(forTaskWithIdentifier: BackgroundSyncRegistry.identifier, using: nil) { task in
            guard let refresh = task as? BGAppRefreshTask else { task.setTaskCompleted(success: false); return }
            BackgroundSyncRegistry.schedule()
            let work = Task {
                await BackgroundSyncRegistry.shared.handler?()
                refresh.setTaskCompleted(success: !Task.isCancelled)
            }
            refresh.expirationHandler = { work.cancel() }
        }
        return true
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        completionHandler([.banner, .list, .sound])
    }

    func applicationDidEnterBackground(_ application: UIApplication) {
        BackgroundSyncRegistry.schedule()
    }
}

final class BackgroundSyncRegistry: @unchecked Sendable {
    static let shared = BackgroundSyncRegistry()
    static let identifier = "com.mouchen.ios.refresh"
    var handler: (@Sendable () async -> Void)?

    static func schedule() {
        let request = BGAppRefreshTaskRequest(identifier: identifier)
        request.earliestBeginDate = Date(timeIntervalSinceNow: 15 * 60)
        try? BGTaskScheduler.shared.submit(request)
    }
}
