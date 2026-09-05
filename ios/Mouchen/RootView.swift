import SwiftUI

struct RootView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        Group {
            if model.configuration == nil {
                SetupView()
            } else {
                MainTabs()
            }
        }
        .environment(\.locale, Locale(identifier: model.locale.rawValue))
        .alert(model.t("AI替身提示", "My AI Twin"), isPresented: Binding(
            get: { model.lastError != nil },
            set: { if !$0 { model.lastError = nil } }
        )) {
            Button(model.t("知道了", "OK"), role: .cancel) { model.lastError = nil }
        } message: {
            Text(model.lastError ?? model.t("未知错误", "Unknown error"))
        }
    }
}

private struct SetupView: View {
    @EnvironmentObject private var model: AppModel
    @State private var serverURL = AppModel.suggestedServerURL
    @State private var username = ""
    @State private var password = ""
    @State private var registrationCode = ""
    @State private var createAccount = false
    // A commercial account must explicitly opt in before any cloud analysis.
    @State private var proactiveCloud = false
    @State private var fullContext = false
    @State private var connecting = false

    var body: some View {
        NavigationStack {
            Form {
                Picker(model.t("语言", "Language"), selection: Binding(
                    get: { model.locale },
                    set: { model.selectUnauthenticatedLocale($0) }
                )) {
                    ForEach(AppLocale.allCases) { locale in
                        Text(locale.displayName).tag(locale)
                    }
                }
                .pickerStyle(.segmented)

                Picker(model.t("进入方式", "Continue with"), selection: $createAccount) {
                    Text(model.t("登录", "Sign in")).tag(false)
                    Text(model.t("注册", "Register")).tag(true)
                }
                .pickerStyle(.segmented)

                Section {
                    TextField(model.t("https://服务器地址", "https://server-address"), text: $serverURL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .keyboardType(.URL)
                    TextField(model.t("用户名", "Username"), text: $username)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    SecureField(model.t("密码", "Password"), text: $password)
                    if createAccount {
                        SecureField(model.t("邀请码（内测注册需要）", "Invitation code (required for private beta)"), text: $registrationCode)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                    }
                } header: {
                    Text(createAccount ? model.t("创建AI替身账号", "Create a My AI Twin account") : model.t("登录AI替身", "Sign in to My AI Twin"))
                } footer: {
                    Text(model.t(
                        "Windows、Android 和 iPhone 登录同一账号后自动共享目标、事件、谏言和反馈；设备访问凭证分别保存，可单独退出。",
                        "Sign in with the same account on Windows, Android, and iPhone to share goals, events, counsel, and feedback. Each device keeps its own credential and can sign out independently."
                    ))
                }

                Section(model.t("云端推理", "Cloud reasoning")) {
                    Toggle(model.t("允许主动云分析", "Allow proactive cloud analysis"), isOn: $proactiveCloud)
                    Toggle(model.t("上传获授权的完整文本", "Upload full text from authorized sources"), isOn: $fullContext)
                    Text(fullContext
                         ? model.t("授权来源的正文可进入云端。", "Full text from authorized sources may be sent to the cloud.")
                         : model.t("默认只上传时间、类型和必要摘要。", "By default, only time, type, and the minimum required summary are uploaded."))
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }

                Section {
                    Button {
                        connecting = true
                        Task {
                            _ = await model.authenticate(
                                serverURL: serverURL,
                                username: username,
                                password: password,
                                registrationCode: registrationCode.isEmpty ? nil : registrationCode,
                                createAccount: createAccount,
                                proactiveCloudEnabled: proactiveCloud,
                                uploadFullContext: fullContext
                            )
                            connecting = false
                        }
                    } label: {
                        HStack {
                            Spacer()
                            if connecting {
                                ProgressView()
                            } else {
                                Text(createAccount ? model.t("注册并登录", "Register and sign in") : model.t("登录", "Sign in"))
                            }
                            Spacer()
                        }
                    }
                    .disabled(
                        connecting
                            || username.trimmingCharacters(in: .whitespacesAndNewlines).count < 3
                            || password.count < 10
                    )
                }
            }
            .navigationTitle(model.t("AI替身", "My AI Twin"))
        }
    }
}

private struct MainTabs: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        TabView {
            SituationView()
                .tabItem { Label(model.t("局势", "Situation"), systemImage: "scope") }
            AdviceListView()
                .tabItem { Label(model.t("谏言", "Counsel"), systemImage: "bell.badge") }
            GoalsView()
                .tabItem { Label(model.t("目标", "Goals"), systemImage: "flag.checkered") }
            CaptureView()
                .tabItem { Label(model.t("问策", "Ask"), systemImage: "sparkles") }
            SettingsView()
                .tabItem { Label(model.t("设置", "Settings"), systemImage: "gearshape") }
        }
    }
}

private struct SituationView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        NavigationStack {
            List {
                Section(model.t("云端状态", "Cloud status")) {
                    LabeledContent(model.t("连接", "Connection"), value: model.healthText)
                    LabeledContent(model.t("待同步事件", "Pending events"), value: "\(model.pendingEvents)")
                    if let date = model.lastSyncAt {
                        LabeledContent(model.t("上次同步", "Last sync")) { Text(date, style: .relative) }
                    }
                    Button {
                        Task { await model.sync() }
                    } label: {
                        Label(
                            model.isSyncing ? model.t("正在同步", "Syncing") : model.t("立即同步并研判", "Sync and analyze now"),
                            systemImage: "arrow.triangle.2.circlepath"
                        )
                    }
                    .disabled(model.isSyncing)
                }

                Section(model.t("获授权的信息", "Authorized information")) {
                    Button { Task { await model.collectCalendar() } } label: {
                        Label(model.t("读取近 7 天和未来 21 天日历", "Read calendar: past 7 and next 21 days"), systemImage: "calendar")
                    }
                    Button { Task { await model.collectContacts() } } label: {
                        Label(model.t("更新联系人局势快照", "Update contacts context snapshot"), systemImage: "person.2")
                    }
                    Button { Task { await model.collectLocation() } } label: {
                        Label(model.t("提交一次当前位置", "Submit current location once"), systemImage: "location")
                    }
                    Text(model.t(
                        "网页、聊天或文档中的问题，请从该 App 的“分享”菜单选择“发送到AI替身”。",
                        "For a problem in a webpage, chat, or document, use that app's Share menu and choose Send to My AI Twin."
                    ))
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                }

                Section(model.t("当前态势", "Current picture")) {
                    LabeledContent(model.t("目标", "Goals"), value: "\(model.goals.count)")
                    LabeledContent(model.t("有效谏言", "Active counsel"), value: "\(model.advice.filter { $0.status == "active" }.count)")
                    Text(model.t(
                        "iOS 不允许普通 App 全局读取其他 App、完整微信历史或持续后台录屏；AI替身通过分享、日历和显式采集补足。",
                        "iOS does not let ordinary apps read every other app, full private chat history, or continuously record the screen in the background. My AI Twin uses sharing, calendar access, and explicit collection instead."
                    ))
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                }
            }
            .navigationTitle(model.t("局势", "Situation"))
            .refreshable { await model.sync() }
        }
    }
}

private struct AdviceListView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        NavigationStack {
            List {
                if model.advice.isEmpty {
                    ContentUnavailableView(
                        model.t("暂无谏言", "No counsel yet"),
                        systemImage: "bell.slash",
                        description: Text(model.t("先建立一条目标，并同步已授权信息。", "Create a goal, then sync authorized information."))
                    )
                }
                ForEach(model.advice) { item in
                    AdviceCard(item: item)
                }
            }
            .navigationTitle(model.t("谏言", "Counsel"))
            .refreshable { await model.sync() }
        }
    }
}

private struct AdviceCard: View {
    @EnvironmentObject private var model: AppModel
    let item: Advice

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("L\(item.effectiveLevel.rawValue) · \(item.domain)")
                    .font(.headline)
                Spacer()
                Text(localizedStatus(item.status))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            // Advice content is generated by the server in the account locale.
            // Goal quotes and evidence stay verbatim in every interface language.
            Text(item.action).font(.body)
            Label(item.firstStep, systemImage: "figure.walk")
            Text(model.t(
                "依据你的原话：“\(item.goalQuote)”",
                "In your own words: “\(item.goalQuote)”"
            ))
                .font(.footnote)
                .foregroundStyle(.secondary)
            Text(model.t(
                "预测：\(item.prediction.outcome)（期限 \(item.prediction.deadline)，置信度 \(Int(item.prediction.confidence * 100))%）",
                "Prediction: \(item.prediction.outcome) (deadline: \(item.prediction.deadline), confidence: \(Int(item.prediction.confidence * 100))%)"
            ))
            .font(.footnote)
            if let alternative = item.alternative, !alternative.isEmpty {
                Text(model.t("替代路径：\(alternative)", "Alternative: \(alternative)")).font(.footnote)
            }
            HStack {
                Button(model.t("采纳", "Adopt")) { Task { await model.sendFeedback(item, kind: "adopted") } }
                    .buttonStyle(.borderedProminent)
                Button(model.t("撤下", "Dismiss")) { Task { await model.sendFeedback(item, kind: "dismissed") } }
                    .buttonStyle(.bordered)
            }
            HStack {
                Button(model.t("预测已应验", "Prediction correct")) { Task { await model.verify(item, correct: true) } }
                Button(model.t("预测错误", "Prediction incorrect")) { Task { await model.verify(item, correct: false) } }
                    .foregroundStyle(.red)
            }
            .font(.caption)
        }
        .padding(.vertical, 4)
    }

    private func localizedStatus(_ value: String) -> String {
        switch value.lowercased() {
        case "active": model.t("有效", "Active")
        case "adopted": model.t("已采纳", "Adopted")
        case "dismissed": model.t("已撤下", "Dismissed")
        case "expired": model.t("已过期", "Expired")
        default: value
        }
    }
}

private struct GoalsView: View {
    @EnvironmentObject private var model: AppModel
    @State private var showingAdd = false

    var body: some View {
        NavigationStack {
            List(model.goals) { goal in
                VStack(alignment: .leading, spacing: 6) {
                    HStack {
                        Text(goal.title).font(.headline)
                        if goal.isRedline {
                            Text(model.t("红线", "Red line")).font(.caption).foregroundStyle(.red)
                        }
                    }
                    Text(goal.domain).font(.caption).foregroundStyle(.secondary)
                    Text("“\(goal.quote)”")
                }
            }
            .overlay {
                if model.goals.isEmpty {
                    ContentUnavailableView(
                        model.t("尚无目标", "No goals yet"),
                        systemImage: "flag",
                        description: Text(model.t("主动研判必须先知道你亲口确定的方向。", "Proactive analysis needs a direction you explicitly set."))
                    )
                }
            }
            .navigationTitle(model.t("目标", "Goals"))
            .toolbar {
                Button { showingAdd = true } label: { Label(model.t("新增", "Add"), systemImage: "plus") }
            }
            .sheet(isPresented: $showingAdd) { AddGoalView(isPresented: $showingAdd) }
        }
    }
}

private struct AddGoalView: View {
    @EnvironmentObject private var model: AppModel
    @Binding var isPresented: Bool
    @State private var domain = "work"
    @State private var title = ""
    @State private var quote = ""
    @State private var weeklyHours = ""
    @State private var redline = false

    var body: some View {
        NavigationStack {
            Form {
                TextField(model.t("领域，例如 work / health", "Domain, e.g. work / health"), text: $domain)
                    .textInputAutocapitalization(.never)
                TextField(model.t("目标标题", "Goal title"), text: $title)
                TextField(model.t("你的目标原话", "Your goal in your own words"), text: $quote, axis: .vertical)
                TextField(model.t("每周投入小时（可选）", "Hours per week (optional)"), text: $weeklyHours)
                    .keyboardType(.decimalPad)
                Toggle(model.t("授权为红线域", "Authorize as a red-line domain"), isOn: $redline)
            }
            .navigationTitle(model.t("建立目标", "Create goal"))
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button(model.t("取消", "Cancel")) { isPresented = false }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button(model.t("保存", "Save")) {
                        Task {
                            if await model.addGoal(
                                domain: domain,
                                title: title,
                                quote: quote,
                                weeklyHours: Double(weeklyHours),
                                redline: redline
                            ) { isPresented = false }
                        }
                    }
                    .disabled(title.trimmingCharacters(in: .whitespaces).isEmpty || quote.trimmingCharacters(in: .whitespaces).isEmpty)
                }
            }
        }
    }
}

private struct CaptureView: View {
    @EnvironmentObject private var model: AppModel
    @State private var problem = ""
    @State private var domain = "general"
    @State private var question = ""

    var body: some View {
        NavigationStack {
            Form {
                Section(model.t("报告正在发生的问题", "Report a current problem")) {
                    TextField(model.t("领域，例如 work / finance", "Domain, e.g. work / finance"), text: $domain)
                        .textInputAutocapitalization(.never)
                    TextEditor(text: $problem).frame(minHeight: 100)
                    Button(model.t("记录并立即研判", "Record and analyze now")) {
                        let submitted = problem
                        problem = ""
                        Task {
                            await model.recordProblem(submitted, domain: domain)
                            await model.sync()
                        }
                    }
                    .disabled(problem.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }

                Section(model.t("直接问策", "Ask directly")) {
                    TextField(model.t("输入问题", "Enter a question"), text: $question, axis: .vertical)
                    Button(model.t("调用AI替身模型", "Ask My AI Twin")) { Task { await model.ask(question) } }
                        .disabled(question.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    if !model.answer.isEmpty { Text(model.answer).textSelection(.enabled) }
                }
            }
            .navigationTitle(model.t("问策", "Ask"))
        }
    }
}

private struct SettingsView: View {
    @EnvironmentObject private var model: AppModel
    @State private var draft: ServerConfiguration?

    var body: some View {
        NavigationStack {
            Form {
                Section(model.t("语言", "Language")) {
                    Picker(model.t("界面和新建言", "Interface and new counsel"), selection: Binding(
                        get: { model.locale },
                        set: { value in Task { await model.updateLocale(value) } }
                    )) {
                        ForEach(AppLocale.allCases) { locale in
                            Text(locale.displayName).tag(locale)
                        }
                    }
                    .disabled(model.isChangingLocale)
                    if model.isChangingLocale { ProgressView() }
                    Text(model.t(
                        "此设置属于账号，会同步到其他设备。已有建言和证据保留原文；新建言按所选语言生成。",
                        "This account setting syncs to your other devices. Existing counsel and evidence stay in their original language; new counsel uses the selected language."
                    ))
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                }

                if let binding = Binding($draft) {
                    Section(model.t("账号", "Account")) {
                        LabeledContent(model.t("用户名", "Username"), value: binding.wrappedValue.username ?? model.t("已登录", "Signed in"))
                        LabeledContent(model.t("服务器", "Server"), value: binding.wrappedValue.baseURL)
                    }
                    Section(model.t("数据出口", "Data sharing")) {
                        Toggle(model.t("允许主动云分析", "Allow proactive cloud analysis"), isOn: binding.proactiveCloudEnabled)
                        Toggle(model.t("上传获授权的完整文本", "Upload full text from authorized sources"), isOn: binding.uploadFullContext)
                    }
                    Button(model.t("保存并验证", "Save and verify")) {
                        guard let draft else { return }
                        Task { _ = await model.saveConfiguration(draft) }
                    }
                }
                Section(model.t("本机数据", "On-device data")) {
                    Button(model.t("清空待同步队列", "Clear pending queue"), role: .destructive) { Task { await model.purgeLocalQueue() } }
                    Button(model.t("退出当前设备", "Sign out on this device"), role: .destructive) { Task { await model.logout() } }
                }
            }
            .navigationTitle(model.t("设置", "Settings"))
            .onAppear { draft = model.configuration }
        }
    }
}
