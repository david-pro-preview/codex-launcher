import AppKit
import SwiftUI
import Combine

struct LauncherButtonStyle: ButtonStyle {
    var highlight = true
    var padding: CGFloat = 0
    func makeBody(configuration: Configuration) -> some View {
        HoverButtonBody(configuration: configuration, highlight: highlight, padding: padding)
    }
    private struct HoverButtonBody: View {
        let configuration: ButtonStyle.Configuration
        let highlight: Bool
        let padding: CGFloat
        @Environment(\.isEnabled) private var isEnabled
        @State private var hovering = false
        var body: some View {
            configuration.label
                .padding(padding)
                .background(RoundedRectangle(cornerRadius: 6).fill(Color.primary.opacity(
                    highlight && isEnabled ? (configuration.isPressed ? 0.13 : hovering ? 0.10 : 0) : 0)))
                .contentShape(RoundedRectangle(cornerRadius: 6))
                .opacity(isEnabled ? 1 : 0.45)
                .onHover { hovering = $0 }
        }
    }
}

struct UsageWindow: Decodable, Identifiable, Equatable {
    let id: String
    let usedPercent: Double
    let resetsAt: Double
    let durationSeconds: Double
    var label: String {
        if durationSeconds == 604800 { return "Weekly" }
        if durationSeconds.truncatingRemainder(dividingBy: 3600) == 0 { return "\(Int(durationSeconds / 3600))h" }
        return "\(Int(durationSeconds / 60))m"
    }
    func elapsed(at now: Double) -> Double { min(1, max(0, 1 - (resetsAt - now) / durationSeconds)) }
    func countdown(at now: Double) -> String {
        let minutes = Int(ceil(max(0, resetsAt - now) / 60))
        if minutes == 0 { return "等待更新" }
        if minutes >= 1440 { return "\(minutes / 1440)d \((minutes % 1440) / 60)h 后重置" }
        if minutes >= 60 { return "\(minutes / 60)h \(minutes % 60)m 后重置" }
        return "\(minutes)m 后重置"
    }
}

struct UsageSnapshot: Decodable, Equatable {
    let windows: [UsageWindow]
    let fetchedAt: Double
}

struct Account: Decodable, Identifiable, Equatable {
    let id: String
    let name: String
    let email: String
    let avatarPath: String?
    let isCurrent: Bool
    let plan: String
    var usage: UsageSnapshot? = nil
    var usageError: String? = nil
    var rowHeight: CGFloat {
        let count = max(1, usage?.windows.count ?? 0)
        return max(94, CGFloat(count * 74 + 22 + (usageError == nil ? 0 : 30)))
    }
}

struct BackendEvent: Decodable {
    let kind: String
    var message: String?
    var phase: String?
    var url: String?
    var code: String?
    var refreshedAt: Double?
    var accounts: [Account]?
}

final class LauncherModel: ObservableObject {
    @Published var accounts: [Account] = []
    @Published var refreshedAt: Double = 0
    @Published var working = false
    @Published var waitingForLogin = false
    @Published var message = ""
    @Published var isError = false
    @Published var loginURL: URL?
    var process: Process?
    var demo = false
    var afterFinish: (() -> Void)?
    private var receivedResult = false
    private var receivedError = false
    private var pendingAction: (String, String?)?
    private var refreshAfterList = false

    init(demo: Bool = false) {
        self.demo = demo
        if demo {
            refreshedAt = Date().timeIntervalSince1970 - 120
            accounts = [
                Account(id: "demo-a", name: "Alex Chen", email: "alex@example.com", avatarPath: nil, isCurrent: true, plan: "Pro 20x", usage: UsageSnapshot(windows: [UsageWindow(id: "weekly", usedPercent: 28, resetsAt: Date().timeIntervalSince1970 + 345600, durationSeconds: 604800)], fetchedAt: Date().timeIntervalSince1970)),
                Account(id: "demo-b", name: "Morgan Lee", email: "morgan@example.com", avatarPath: nil, isCurrent: false, plan: "Plus", usage: UsageSnapshot(windows: [UsageWindow(id: "short", usedPercent: 64, resetsAt: Date().timeIntervalSince1970 + 7200, durationSeconds: 18000), UsageWindow(id: "weekly", usedPercent: 12, resetsAt: Date().timeIntervalSince1970 + 518400, durationSeconds: 604800)], fetchedAt: Date().timeIntervalSince1970))
            ]
        }
    }

    func start() {
        if demo { return }
        refreshAfterList = true
        run("list")
    }

    func run(_ action: String, id: String? = nil) {
        guard !demo else { return }
        if process != nil {
            if action != "list" && action != "poll" && action != "usage" { pendingAction = (action, id) }
            return
        }
        guard let resource = Bundle.main.url(forResource: "account_manager", withExtension: "py") else {
            message = "启动器组件缺失，请重新安装。"; isError = true; return
        }
        let candidates = [
            "/opt/homebrew/bin/python3", "/usr/local/bin/python3",
            FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3").path,
            "/Library/Developer/CommandLineTools/usr/bin/python3"
        ]
        guard let python = candidates.first(where: { FileManager.default.isExecutableFile(atPath: $0) }) else {
            message = "未找到可运行的 Python 3。请安装 Python 3 或恢复 Codex 的运行时。"; isError = true; return
        }
        let task = Process()
        task.executableURL = URL(fileURLWithPath: python)
        task.arguments = [resource.path, action] + (id.map { ["--id", $0] } ?? [])
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONUNBUFFERED"] = "1"
        task.environment = environment
        let output = Pipe()
        task.standardOutput = output
        task.standardError = FileHandle.nullDevice
        task.standardInput = FileHandle.nullDevice
        process = task
        working = action != "list" && action != "poll" && action != "usage"
        waitingForLogin = false
        loginURL = nil
        receivedResult = false
        receivedError = false
        if action != "list" && action != "poll" && action != "usage" {
            isError = false
            message = action == "refresh" ? "正在更新账号资料…" : "正在准备…"
        }
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                try task.run()
                var buffer = Data()
                while let chunk = try output.fileHandleForReading.read(upToCount: 8192), !chunk.isEmpty {
                    buffer.append(chunk)
                    while let newline = buffer.firstIndex(of: 10) {
                        let line = Data(buffer[..<newline])
                        buffer.removeSubrange(...newline)
                        if let event = try? JSONDecoder().decode(BackendEvent.self, from: line) {
                            DispatchQueue.main.async { self?.receive(event) }
                        }
                    }
                }
                task.waitUntilExit()
                DispatchQueue.main.async { self?.finish(action, status: task.terminationStatus) }
            } catch {
                DispatchQueue.main.async {
                    self?.message = "无法运行账号管理组件。请确认本机已安装 Python 3。"
                    self?.isError = true
                    self?.receivedError = true
                    self?.finish(action, status: 1)
                }
            }
        }
    }

    private func receive(_ event: BackendEvent) {
        if let refreshedAt = event.refreshedAt { self.refreshedAt = refreshedAt }
        switch event.kind {
        case "progress":
            message = event.message ?? "正在处理…"
            waitingForLogin = event.phase == "login"
            isError = false
        case "loginURL":
            if let raw = event.url, let url = URL(string: raw), url.scheme == "https", url.host == "auth.openai.com" {
                loginURL = url
            }
        case "result":
            if let loaded = event.accounts { accounts = loaded }
            if let text = event.message, !text.isEmpty {
                message = text
                isError = false
            } else if !isError {
                message = ""
            }
            receivedResult = true
        case "error":
            if event.code == "busy" {
                // A second launcher operation holds the private storage lock.
                message = "另一个账号操作正在进行，请稍后重试。"
                isError = false
            } else {
                message = event.message ?? "操作没有完成，请重试。"
                isError = event.code != "cancelled"
            }
            receivedError = true
        default: break
        }
    }

    private func finish(_ action: String, status: Int32) {
        process = nil
        working = false
        waitingForLogin = false
        loginURL = nil
        if !receivedResult && !receivedError {
            message = "操作没有完成，请重试。"
            isError = true
        }
        if let callback = afterFinish {
            afterFinish = nil
            callback()
            return
        }
        if let pending = pendingAction {
            pendingAction = nil
            run(pending.0, id: pending.1)
        } else if action == "list" && refreshAfterList && status == 0 {
            refreshAfterList = false
            run("poll")
        } else if (action == "add" || action == "switch") && status != 0 {
            run("list")
        }
    }

    func cancelLogin() {
        guard waitingForLogin, let process = process, process.isRunning else { return }
        message = "正在取消并恢复原账号…"
        waitingForLogin = false
        process.interrupt()
    }
}

struct AvatarView: View {
    let account: Account
    private var initials: String {
        let words = account.name.split(separator: " ")
        if words.count > 1 { return String(words.first!.prefix(1) + words.last!.prefix(1)).uppercased() }
        return String(account.name.prefix(1)).uppercased()
    }
    private var tint: Color {
        let colors: [Color] = [Color(red: 0.20, green: 0.49, blue: 0.43), Color(red: 0.39, green: 0.40, blue: 0.69), Color(red: 0.67, green: 0.41, blue: 0.30), Color(red: 0.30, green: 0.47, blue: 0.65)]
        return colors[account.id.utf8.reduce(0) { $0 + Int($1) } % colors.count]
    }
    var body: some View {
        Group {
            if let path = account.avatarPath, let image = NSImage(contentsOfFile: path) {
                Image(nsImage: image).resizable().scaledToFill()
            } else {
                ZStack {
                    LinearGradient(colors: [tint.opacity(0.85), tint], startPoint: .topLeading, endPoint: .bottomTrailing)
                    Text(initials).font(.system(size: 16, weight: .semibold, design: .rounded)).foregroundColor(.white)
                }
            }
        }
        .frame(width: 42, height: 42)
        .clipShape(Circle())
        .overlay(Circle().stroke(Color.primary.opacity(0.06), lineWidth: 1))
        .accessibilityHidden(true)
    }
}

struct UsageRing: View {
    let remaining: Double
    let elapsed: Double
    let stale: Bool
    let resetText: String
    private let ringWidth: CGFloat = 6
    private let tint = Color(red: 0.27, green: 0.64, blue: 0.69)
    var body: some View {
        ZStack {
            Circle().stroke(tint.opacity(0.16), lineWidth: ringWidth)
            Circle().trim(from: 0, to: min(1, max(0, remaining)))
                .stroke(stale ? Color.secondary : tint, style: StrokeStyle(lineWidth: ringWidth, lineCap: .butt))
                .rotationEffect(.degrees(-90))
            VStack(spacing: 1) {
                Text("\(Int((remaining * 100).rounded()))%")
                    .font(.system(size: 13, weight: .semibold, design: .rounded)).foregroundColor(.primary)
                Text(resetText).font(.system(size: 7.5)).foregroundColor(.secondary)
                    .lineLimit(1).minimumScaleFactor(0.8)
            }.frame(width: 44)
            Rectangle().fill(Color(nsColor: .windowBackgroundColor))
                .frame(width: 4, height: ringWidth)
                .overlay(Rectangle().fill(Color.red).frame(width: 2, height: ringWidth))
                .offset(y: -26)
                .rotationEffect(.degrees(360 * elapsed))
        }
        .frame(width: 52, height: 52).padding(4)
        .accessibilityHidden(true)
    }
}

struct UsageView: View {
    let account: Account
    var body: some View {
        TimelineView(.periodic(from: .now, by: 30)) { context in
            let now = context.date.timeIntervalSince1970
            VStack(alignment: .leading, spacing: 5) {
                if let usage = account.usage {
                    VStack(spacing: 5) {
                        ForEach(usage.windows) { window in
                            let stale = account.usageError != nil || now - usage.fetchedAt > 660 || now >= window.resetsAt
                            VStack(spacing: 0) {
                                UsageRing(remaining: (100 - window.usedPercent) / 100,
                                          elapsed: window.elapsed(at: now), stale: stale,
                                          resetText: window.countdown(at: now).replacingOccurrences(of: " 后重置", with: ""))
                                Text(window.label).font(.system(size: 8, weight: .medium)).foregroundColor(.secondary)
                            }
                            .opacity(stale ? 0.6 : 1)
                            .help("剩余额度 \(Int(100 - window.usedPercent))%；中心下方为重置倒计时；红线：周期已过 \(Int(window.elapsed(at: now) * 100))%；重置：\(Date(timeIntervalSince1970: window.resetsAt).formatted(date: .abbreviated, time: .shortened))")
                        }
                    }
                    if account.usageError != nil || now - usage.fetchedAt > 660 {
                        Text(account.usageError ?? "数据待刷新").font(.system(size: 9)).foregroundColor(.orange)
                            .help("上次更新：\(Date(timeIntervalSince1970: usage.fetchedAt).formatted())")
                    }
                } else {
                    Text(account.usageError ?? "正在读取用量…")
                        .font(.system(size: 10)).foregroundColor(.secondary).fixedSize(horizontal: false, vertical: true)
                }
            }
        }.frame(width: 66)
    }
}

struct AccountRow: View {
    let account: Account
    let disabled: Bool
    let action: () -> Void
    @State private var hovering = false
    private let accent = Color(red: 0.12, green: 0.55, blue: 0.42)
    var body: some View {
        Button(action: action) {
            HStack(spacing: 9) {
                AvatarView(account: account)
                VStack(alignment: .leading, spacing: 4) {
                    HStack(spacing: 6) {
                        Text(account.name).font(.system(size: 14, weight: .semibold)).lineLimit(1).foregroundColor(.primary)
                        Text(account.plan.isEmpty ? "未知套餐" : account.plan)
                            .font(.system(size: 9, weight: .semibold)).foregroundColor(accent)
                            .padding(.horizontal, 6).padding(.vertical, 3)
                            .background(Capsule().fill(accent.opacity(0.09))).fixedSize()
                    }
                    Text(account.email.isEmpty ? "ChatGPT 账号" : account.email)
                        .font(.system(size: 11.5)).foregroundColor(.secondary).lineLimit(1).truncationMode(.middle)
                    if account.isCurrent {
                        HStack(alignment: .center, spacing: 3) {
                            Image(systemName: "checkmark.circle.fill")
                                .font(.system(size: 9)).frame(width: 10, height: 12)
                            Text("当前").font(.system(size: 9, weight: .medium))
                                .frame(height: 12)
                        }.foregroundColor(accent)
                    }
                }
                Spacer(minLength: 0)
                UsageView(account: account)
            }
            .padding(11)
            .frame(height: account.rowHeight)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: 12).fill(account.isCurrent ? accent.opacity(0.055) : Color.primary.opacity(hovering ? 0.06 : 0.02)))
            .overlay(RoundedRectangle(cornerRadius: 12).stroke(account.isCurrent ? accent.opacity(0.2) : Color.primary.opacity(0.035), lineWidth: 1))
            .contentShape(RoundedRectangle(cornerRadius: 12))
        }
        .buttonStyle(LauncherButtonStyle(highlight: false))
        .onHover { hovering = $0 && !disabled && !account.isCurrent }
        .disabled(disabled)
        .accessibilityLabel("\(account.name)，\(account.email)，\(account.isCurrent ? "当前账号，打开 ChatGPT" : "切换到此账号")")
    }
}

struct LauncherView: View {
    @ObservedObject var model: LauncherModel
    var dismiss: () -> Void = {}
    var quit: () -> Void = {}
    @State private var addHovering = false
    private let accent = Color(red: 0.12, green: 0.55, blue: 0.42)
    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 10) {
                if let url = Bundle.main.url(forResource: "AppIcon", withExtension: "png"), let image = NSImage(contentsOf: url) {
                    Image(nsImage: image).resizable().frame(width: 36, height: 36)
                } else {
                    Image(systemName: "arrow.triangle.2.circlepath").font(.system(size: 24)).foregroundColor(accent).frame(width: 36, height: 36)
                }
                VStack(alignment: .leading, spacing: 3) {
                    Text("Codex Launcher").font(.system(size: 16, weight: .semibold))
                    Text("你的账号，随时切换").font(.system(size: 11)).foregroundColor(.secondary)
                }
                Spacer()
            }
            .padding(.horizontal, 18).padding(.top, 18).padding(.bottom, 17)
            Divider().opacity(0.55)
            HStack {
                Text("已登录账号").font(.system(size: 11, weight: .medium)).foregroundColor(.secondary)
                Spacer()
                Text("\(model.accounts.count)").font(.system(size: 10, weight: .medium, design: .rounded)).foregroundColor(.secondary)
            }.padding(.horizontal, 20).padding(.top, 15).padding(.bottom, 9)
            if model.accounts.isEmpty {
                VStack(spacing: 7) {
                    Image(systemName: "person.crop.circle.badge.plus").font(.system(size: 27)).foregroundColor(.secondary)
                    Text("添加一个账号开始使用").font(.system(size: 12)).foregroundColor(.secondary)
                }.frame(maxWidth: .infinity).frame(height: 85)
            } else {
                ScrollView {
                    VStack(spacing: 7) {
                        ForEach(model.accounts) { account in
                            AccountRow(account: account, disabled: model.working) {
                                model.run(account.isCurrent ? "open" : "switch", id: account.isCurrent ? nil : account.id)
                            }
                        }
                    }.padding(.horizontal, 16).padding(.vertical, 1)
                }
                .frame(height: min(460, model.accounts.reduce(CGFloat(0)) { $0 + $1.rowHeight + 7 } - 5))
            }
            Button { model.run("add") } label: {
                HStack(spacing: 8) {
                    Image(systemName: "plus.circle.fill").font(.system(size: 15))
                    Text("添加新账号").font(.system(size: 12.5, weight: .medium))
                    Spacer()
                    Image(systemName: "arrow.up.right").font(.system(size: 10, weight: .medium)).opacity(0.65)
                }
                .foregroundColor(accent)
                .padding(.horizontal, 13).padding(.vertical, 12)
                .background(RoundedRectangle(cornerRadius: 11).fill(accent.opacity(addHovering && !model.working ? 0.11 : 0.065)))
                .overlay(RoundedRectangle(cornerRadius: 11).stroke(accent.opacity(0.12), lineWidth: 1))
            }
            .buttonStyle(LauncherButtonStyle(highlight: false)).disabled(model.working)
            .onHover { addHovering = $0 }
            .padding(.horizontal, 16).padding(.top, 12).padding(.bottom, 13)
            if model.working || !model.message.isEmpty {
                VStack(alignment: .leading, spacing: 10) {
                    HStack(alignment: .top, spacing: 8) {
                        if model.working {
                            ProgressView().controlSize(.small).scaleEffect(0.8).frame(width: 14, height: 14)
                        } else {
                            Image(systemName: model.isError ? "exclamationmark.circle.fill" : "checkmark.circle.fill")
                                .font(.system(size: 12)).foregroundColor(model.isError ? .orange : accent)
                        }
                        Text(model.message.isEmpty ? "正在准备…" : model.message)
                            .font(.system(size: 11.5)).foregroundColor(.secondary).fixedSize(horizontal: false, vertical: true)
                        Spacer(minLength: 0)
                    }
                    if model.waitingForLogin {
                        HStack(spacing: 12) {
                            if let url = model.loginURL {
                                Button("打开登录页") { NSWorkspace.shared.open(url) }.buttonStyle(.link)
                            }
                            Button("取消登录") { model.cancelLogin() }.buttonStyle(.link)
                        }.font(.system(size: 11.5))
                    }
                }
                .padding(11).frame(maxWidth: .infinity, alignment: .leading)
                .background(RoundedRectangle(cornerRadius: 10).fill(Color.primary.opacity(0.035)))
                .padding(.horizontal, 16).padding(.bottom, 13)
            }
            Divider().opacity(0.55)
            HStack(spacing: 8) {
                Button { model.run("open") } label: {
                    Label("打开 ChatGPT", systemImage: "arrow.up.forward.app").font(.system(size: 11.5, weight: .medium))
                }.buttonStyle(LauncherButtonStyle(highlight: true, padding: 6)).disabled(model.working)
                Spacer()
                TimelineView(.periodic(from: .now, by: 15)) { context in
                    let minutes = Int(max(0, context.date.timeIntervalSince1970 - model.refreshedAt) / 60)
                    Text(model.refreshedAt == 0 ? "尚未刷新" : minutes == 0 ? "刚刚刷新" : minutes < 60 ? "\(minutes) 分钟前刷新" : "\(minutes / 60) 小时前刷新")
                        .font(.system(size: 10)).foregroundColor(.secondary)
                        .help(model.refreshedAt == 0 ? "每 5 分钟自动刷新" : "每 5 分钟自动刷新；上次尝试：\(Date(timeIntervalSince1970: model.refreshedAt).formatted())")
                }
                Button { model.run("refresh") } label: {
                    Image(systemName: "arrow.clockwise").font(.system(size: 12)).frame(width: 20, height: 22)
                }.buttonStyle(LauncherButtonStyle(highlight: true, padding: 6)).disabled(model.working).help("刷新账号资料和用量").accessibilityLabel("刷新账号资料和用量")
                Button(action: quit) { Image(systemName: "power").font(.system(size: 12)).frame(width: 20, height: 22) }
                    .buttonStyle(LauncherButtonStyle(highlight: true, padding: 6)).help("退出启动器").accessibilityLabel("退出启动器")
            }
            .foregroundColor(.secondary).padding(.horizontal, 20).padding(.top, 12).padding(.bottom, 10)
            Text("切换时会关闭 ChatGPT 和 Codex 后台")
                .font(.system(size: 10)).foregroundColor(.secondary.opacity(0.8)).padding(.bottom, 14)
        }
        .frame(width: 370)
        .background(Color(nsColor: .windowBackgroundColor))
        .fixedSize(horizontal: false, vertical: true)
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    var item: NSStatusItem!
    let popover = NSPopover()
    let model = LauncherModel()
    var observer: AnyCancellable?
    var timer: Timer?
    var outsideClickMonitor: Any?
    var localClickMonitor: Any?

    func applicationDidFinishLaunching(_ notification: Notification) {
        let bundleID = Bundle.main.bundleIdentifier ?? "local.kuner.codex-launcher"
        let others = NSRunningApplication.runningApplications(withBundleIdentifier: bundleID).filter { $0.processIdentifier != ProcessInfo.processInfo.processIdentifier }
        if !others.isEmpty {
            DistributedNotificationCenter.default().postNotificationName(NSNotification.Name("CodexLauncher.Show"), object: nil, userInfo: nil, deliverImmediately: true)
            NSApp.terminate(nil)
            return
        }
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        if let button = item.button {
            let url = Bundle.main.url(forResource: "MenuBarTemplate", withExtension: "png")
            let image = url.flatMap { NSImage(contentsOf: $0) } ?? NSImage(systemSymbolName: "arrow.triangle.2.circlepath", accessibilityDescription: nil)!
            image.size = NSSize(width: 18, height: 18)
            image.isTemplate = true
            button.image = image
            button.toolTip = "Codex Launcher"
            button.setAccessibilityLabel("Codex Launcher 账号菜单")
            button.target = self
            button.action = #selector(toggle)
        }
        let view = LauncherView(model: model, dismiss: { [weak self] in self?.popover.performClose(nil) }, quit: { NSApp.terminate(nil) })
        let hosting = NSHostingController(rootView: view)
        hosting.sizingOptions = [.preferredContentSize]
        popover.contentViewController = hosting
        popover.behavior = .transient
        outsideClickMonitor = NSEvent.addGlobalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown, .otherMouseDown]) { [weak self] _ in
            self?.popover.performClose(nil)
        }
        localClickMonitor = NSEvent.addLocalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown, .otherMouseDown]) { [weak self] event in
            guard let self, self.popover.isShown else { return event }
            if event.window !== self.popover.contentViewController?.view.window && event.window !== self.item.button?.window {
                self.popover.performClose(nil)
            }
            return event
        }
        popover.animates = true
        popover.contentSize = NSSize(width: 370, height: 315)
        observer = model.objectWillChange.sink { [weak self, weak hosting] in
            DispatchQueue.main.async {
                guard let self = self, let hosting = hosting else { return }
                hosting.view.layoutSubtreeIfNeeded()
                self.popover.contentSize = hosting.view.fittingSize
                let active = self.model.accounts.first(where: { $0.isCurrent })
                self.item.button?.toolTip = active.map { "Codex Launcher · \($0.name)" } ?? "Codex Launcher"
            }
        }
        DistributedNotificationCenter.default().addObserver(self, selector: #selector(show), name: NSNotification.Name("CodexLauncher.Show"), object: nil)
        timer = Timer.scheduledTimer(withTimeInterval: 300, repeats: true) { [weak self] _ in
            guard let self = self, self.model.process == nil else { return }
            self.model.run("usage")
        }
        model.start()
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in self?.show() }
    }

    @objc func toggle() {
        if popover.isShown { popover.performClose(nil) } else { show() }
    }

    @objc func show() {
        guard let button = item?.button else { return }
        if model.process == nil { model.run("poll") }
        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationDidResignActive(_ notification: Notification) {
        popover.performClose(nil)
    }

    func applicationWillTerminate(_ notification: Notification) {
        if let outsideClickMonitor { NSEvent.removeMonitor(outsideClickMonitor) }
        if let localClickMonitor { NSEvent.removeMonitor(localClickMonitor) }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        show(); return true
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if model.process == nil { return .terminateNow }
        if !model.working { return .terminateNow }
        if model.waitingForLogin {
            let alert = NSAlert()
            alert.messageText = "取消登录并退出启动器？"
            alert.informativeText = "会先恢复原账号，再退出。"
            alert.addButton(withTitle: "取消登录并退出")
            alert.addButton(withTitle: "继续登录")
            if alert.runModal() == .alertFirstButtonReturn {
                model.afterFinish = { NSApp.terminate(nil) }
                model.cancelLogin()
            }
        } else {
            show()
            NSSound.beep()
        }
        return .terminateCancel
    }
}

func renderPreview(_ path: String) {
    let model = LauncherModel(demo: true)
    let hosting = NSHostingView(rootView: LauncherView(model: model))
    hosting.appearance = NSAppearance(named: .aqua)
    hosting.frame = NSRect(x: 0, y: 0, width: 370, height: 450)
    hosting.layoutSubtreeIfNeeded()
    let size = hosting.fittingSize
    hosting.frame = NSRect(origin: .zero, size: size)
    hosting.layoutSubtreeIfNeeded()
    guard let bitmap = hosting.bitmapImageRepForCachingDisplay(in: hosting.bounds) else { exit(1) }
    hosting.cacheDisplay(in: hosting.bounds, to: bitmap)
    guard let png = bitmap.representation(using: .png, properties: [:]) else { exit(1) }
    try! png.write(to: URL(fileURLWithPath: path))
}

let application = NSApplication.shared
application.setActivationPolicy(.accessory)
let arguments = CommandLine.arguments
if let index = arguments.firstIndex(of: "--render-preview"), arguments.count > index + 1 {
    renderPreview(arguments[index + 1])
    exit(0)
}
if arguments.contains("--self-test") {
    let sample = Data("{\"kind\":\"result\",\"accounts\":[{\"id\":\"test\",\"name\":\"测试\",\"email\":\"test@example.com\",\"avatarPath\":null,\"isCurrent\":true,\"plan\":\"pro\"}]}".utf8)
    let event = try! JSONDecoder().decode(BackendEvent.self, from: sample)
    assert(event.accounts?.first?.isCurrent == true)
    assert(event.accounts?.first?.name == "测试")
    let window = UsageWindow(id: "test", usedPercent: 25, resetsAt: 2000, durationSeconds: 1000)
    assert(window.elapsed(at: 1000) == 0)
    assert(window.elapsed(at: 1500) == 0.5)
    assert(window.elapsed(at: 2500) == 1)
    assert(window.countdown(at: 2001) == "等待更新")
    print("Native decoding and cycle progress checks passed")
    exit(0)
}
let delegate = AppDelegate()
application.delegate = delegate
application.run()
