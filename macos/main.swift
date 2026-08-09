// share-it — native macOS shell: floating vibrancy panel + global hotkey (⌥S)
// wrapping the local Python backend's web UI.
import AppKit
import WebKit
import Carbon.HIToolbox
import Quartz
import Security

final class KeyPanel: NSPanel {
    override var canBecomeKey: Bool { true }
}

final class AppDelegate: NSObject, NSApplicationDelegate, WKScriptMessageHandler,
                         NSWindowDelegate, QLPreviewPanelDataSource, QLPreviewPanelDelegate {
    var qlFiles: [URL] = []
    var panel: KeyPanel!
    var webView: WKWebView!
    var server: Process?
    var hotKeyRef: EventHotKeyRef?
    let port = 8749

    func applicationDidFinishLaunching(_ note: Notification) {
        NSApp.setActivationPolicy(.accessory)
        buildMenu()  // without an Edit menu, every ⌘-shortcut beeps in WKWebView
        startServer()
        buildPanel()
        registerHotKey()
        waitForServer(attempts: 60)
    }

    func buildMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem(); main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(NSMenuItem(title: "Quit share-it",
                                   action: #selector(NSApplication.terminate(_:)),
                                   keyEquivalent: "q"))
        appItem.submenu = appMenu
        let editItem = NSMenuItem(); main.addItem(editItem)
        let edit = NSMenu(title: "Edit")
        edit.addItem(NSMenuItem(title: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x"))
        let copyItem = NSMenuItem(title: "Copy", action: #selector(AppDelegate.menuCopy(_:)),
                                 keyEquivalent: "c")
        copyItem.target = self
        edit.addItem(copyItem)
        edit.addItem(NSMenuItem(title: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v"))
        edit.addItem(NSMenuItem(title: "Select All",
                                action: #selector(NSText.selectAll(_:)), keyEquivalent: "a"))
        editItem.submenu = edit
        NSApp.mainMenu = main
    }

    // MARK: backend
    func backendDir() -> String {
        if let res = Bundle.main.resourcePath,
           FileManager.default.fileExists(atPath: res + "/backend/app.py") {
            return res + "/backend"
        }
        // dev fallback: SHAREIT_BACKEND env or source tree next to the binary
        if let env = ProcessInfo.processInfo.environment["SHAREIT_BACKEND"] { return env }
        return FileManager.default.currentDirectoryPath
    }

    func randToken(_ n: Int) -> String {
        var b = [UInt8](repeating: 0, count: n)
        if SecRandomCopyBytes(kSecRandomDefault, n, &b) != errSecSuccess {
            b = (0..<n).map { _ in UInt8.random(in: 0...255) }  // checked fallback
        }
        return Data(b).base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }
    lazy var token: String = randToken(24)
    lazy var readyNonce: String = randToken(18)

    func pythonPath() -> String {
        // prefer the bundled standalone CPython (DMG); fall back to system python3 (dev)
        if let res = Bundle.main.resourcePath {
            let embedded = res + "/python/bin/python3"
            if FileManager.default.isExecutableFile(atPath: embedded) { return embedded }
        }
        return "/usr/bin/env"
    }

    func startServer() {
        let p = Process()
        let py = pythonPath()
        if py == "/usr/bin/env" {
            p.executableURL = URL(fileURLWithPath: "/usr/bin/env")
            p.arguments = ["python3", backendDir() + "/app.py", "--no-browser"]
        } else {
            p.executableURL = URL(fileURLWithPath: py)
            p.arguments = [backendDir() + "/app.py", "--no-browser"]
        }
        var env = ProcessInfo.processInfo.environment
        env["SHAREIT_TOKEN"] = token       // the backend requires exactly this token
        env["SHAREIT_READY"] = readyNonce  // and signals readiness with this nonce
        p.environment = env
        try? FileManager.default.removeItem(atPath: readyPath())  // clear any stale signal
        try? p.run()
        server = p
    }

    func readyPath() -> String {
        return (NSHomeDirectory() as NSString).appendingPathComponent(".shareit/ready")
    }

    func waitForServer(attempts: Int) {
        // wait for OUR child to write its ready nonce after binding the port.
        // We never transmit the token; a squatter can't bind, so it can never
        // produce this file, and we bail instead of trusting it.
        func poll(_ left: Int) {
            let ready = (try? String(contentsOfFile: readyPath(), encoding: .utf8))?
                .trimmingCharacters(in: .whitespacesAndNewlines)
            if ready == readyNonce {
                // ignore any cached copy — a stale page means a stale UI after updates
                webView.load(URLRequest(url: URL(string: "http://127.0.0.1:\(port)/")!,
                                        cachePolicy: .reloadIgnoringLocalAndRemoteCacheData,
                                        timeoutInterval: 30))
                showPanel()
            } else if left > 0 {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { poll(left - 1) }
            } else {
                startupFailed()
            }
        }
        poll(attempts)
    }

    func startupFailed() {
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = "share-it backend didn't start"
        alert.informativeText = "Port \(port) is unreachable or taken by another app. "
            + "Check that python3 ≥ 3.9 is installed, or run `python3 app.py` manually to see the error."
        alert.addButton(withTitle: "Retry")
        alert.addButton(withTitle: "Quit")
        if alert.runModal() == .alertFirstButtonReturn {
            startServer()
            waitForServer(attempts: 40)
        } else {
            NSApp.terminate(nil)
        }
    }

    // MARK: panel
    func buildPanel() {
        let size = NSSize(width: 620, height: 460)
        panel = KeyPanel(contentRect: NSRect(origin: .zero, size: size),
                         styleMask: [.borderless, .nonactivatingPanel],
                         backing: .buffered, defer: false)
        panel.level = .floating
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.hidesOnDeactivate = false
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.delegate = self

        let effect = NSVisualEffectView(frame: NSRect(origin: .zero, size: size))
        // the UI is dark-designed: pin dark vibrancy so a light wallpaper or
        // light system theme can't wash the panel out (Raycast does the same)
        effect.appearance = NSAppearance(named: .darkAqua)
        effect.material = .hudWindow
        effect.blendingMode = .behindWindow
        effect.state = .active
        effect.wantsLayer = true
        effect.layer?.cornerRadius = 18
        effect.layer?.masksToBounds = true
        effect.autoresizingMask = [.width, .height]

        let cfg = WKWebViewConfiguration()
        cfg.userContentController.add(self, name: "shareit")
        // deliver the API token out-of-band (JS injection), so it never travels
        // over HTTP where any local process could read it from the page source
        let inject = WKUserScript(source: "window.__SHAREIT_TOKEN=\"\(token)\";",
                                  injectionTime: .atDocumentStart, forMainFrameOnly: true)
        cfg.userContentController.addUserScript(inject)
        webView = WKWebView(frame: effect.bounds, configuration: cfg)
        webView.setValue(false, forKey: "drawsBackground")
        webView.autoresizingMask = [.width, .height]
        effect.addSubview(webView)
        panel.contentView = effect
    }

    func showPanel() {
        guard let screen = NSScreen.main else { return }
        let f = screen.visibleFrame
        let s = panel.frame.size
        panel.setFrameOrigin(NSPoint(x: f.midX - s.width / 2,
                                     y: f.minY + (f.height - s.height) * 0.62))
        panel.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc func toggle() {
        if panel.isVisible { panel.orderOut(nil) } else { showPanel() }
    }

    var qlOpenedAt: TimeInterval = 0
    func windowDidResignKey(_ notification: Notification) {
        // don't vanish beneath our own Quick Look panel. isVisible lags the
        // key-window switch, so a just-opened QL also counts.
        if QLPreviewPanel.sharedPreviewPanelExists(),
           QLPreviewPanel.shared().isVisible
           || Date().timeIntervalSince1970 - qlOpenedAt < 1.5 { return }
        panel.orderOut(nil)
    }

    // MARK: JS bridge
    func userContentController(_ ucc: WKUserContentController, didReceive message: WKScriptMessage) {
        // defense in depth: ignore bridge calls from any page that isn't ours
        let host = message.frameInfo.request.url?.host
        guard host == "127.0.0.1" || host == "localhost" else { return }
        if message.body as? String == "hide" { panel.orderOut(nil); return }
        guard let dict = message.body as? [String: Any],
              let cmd = dict["cmd"] as? String else { return }
        let files = (dict["files"] as? [String] ?? []).map { URL(fileURLWithPath: $0) }
        // every copy command acknowledges its actual writeObjects result back
        // to the page, so a failed write toasts as a failure, never a success
        func ack(_ ok: Bool) {
            if let id = dict["ack"] as? Int {
                webView.evaluateJavaScript("window.__pbAck && window.__pbAck(\(id), \(ok))")
            }
        }
        switch cmd {
        case "copyText":  // plain text via native pasteboard — WKWebView's
            // navigator.clipboard fails without focus, this never does
            let pb = NSPasteboard.general
            // localOnly: private transcript text / host-only paths must not
            // ride Universal Clipboard to the user's other devices
            if dict["localOnly"] as? Bool == true {
                pb.prepareForNewContents(with: .currentHostOnly)
            } else {
                pb.clearContents()
            }
            ack(pb.setString(dict["text"] as? String ?? "", forType: .string))
        case "copyFiles":  // real file objects on the pasteboard — Finder-grade paste
            let pb = NSPasteboard.general
            pb.prepareForNewContents(with: .currentHostOnly)  // paths are host-only
            ack(pb.writeObjects(files as [NSPasteboardWriting]))
        case "copyRich":
            // one text item carrying three flavors of the SAME content — the
            // receiving app picks: html (Slack/Gmail), rtf (Mail/Notes),
            // plaintext = the markdown itself (editors/terminal)
            let html = dict["html"] as? String ?? ""
            let markdown = dict["markdown"] as? String ?? ""
            let images = (dict["images"] as? [String] ?? []).map { URL(fileURLWithPath: $0) }
            let pb = NSPasteboard.general
            if files.isEmpty && images.isEmpty {
                pb.clearContents()
            } else {
                pb.prepareForNewContents(with: .currentHostOnly)  // file paths ride along
            }
            var items: [NSPasteboardWriting] = []
            let text = NSPasteboardItem()
            if !html.isEmpty {
                text.setString(html, forType: .html)
                // html is produced by our escape-first renderer and carries no
                // <img>, so this conversion never loads a remote/local resource
                if let attr = NSAttributedString(html: Data(html.utf8), documentAttributes: nil),
                   let rtf = attr.rtf(from: NSRange(location: 0, length: attr.length),
                                      documentAttributes: [:]) {
                    text.setData(rtf, forType: .rtf)
                }
            }
            text.setString(markdown.isEmpty ? html : markdown, forType: .string)
            items.append(text)
            for img in images {   // separate items: apps inline or attach natively
                if let data = try? Data(contentsOf: img) {
                    let it = NSPasteboardItem()
                    let uti: String = [
                        "png": "public.png", "jpg": "public.jpeg", "jpeg": "public.jpeg",
                        "gif": "com.compuserve.gif", "webp": "org.webmproject.webp",
                    ][img.pathExtension.lowercased()] ?? "public.png"
                    let type = NSPasteboard.PasteboardType(uti)
                    it.setData(data, forType: type)
                    items.append(it)
                }
            }
            items.append(contentsOf: files as [NSPasteboardWriting])
            ack(pb.writeObjects(items))
        case "quickLook":
            qlOpenedAt = Date().timeIntervalSince1970
            qlFiles = files
            if let ql = QLPreviewPanel.shared() {
                ql.dataSource = self
                ql.delegate = self
                ql.makeKeyAndOrderFront(nil)
                ql.reloadData()
                NotificationCenter.default.addObserver(
                    forName: NSWindow.willCloseNotification, object: ql, queue: .main) { [weak self] _ in
                    self?.panel.makeKeyAndOrderFront(nil)
                    self?.webView.evaluateJavaScript("window.__qlClosed && window.__qlClosed()")
                }
            }
        case "closeQuickLook":
            if QLPreviewPanel.sharedPreviewPanelExists() {
                QLPreviewPanel.shared().orderOut(nil)
            }
        case "hide":
            panel.orderOut(nil)
        default:
            break
        }
    }

    @objc func menuCopy(_ sender: Any?) {
        // route ⌘C into the page: chips → files, selection → text, row → link/title
        webView.evaluateJavaScript("window.__menuCopy && window.__menuCopy()")
    }

    func numberOfPreviewItems(in panel: QLPreviewPanel!) -> Int { qlFiles.count }
    func previewPanel(_ panel: QLPreviewPanel!, previewItemAt index: Int) -> QLPreviewItem! {
        qlFiles[index] as NSURL
    }

    // MARK: global hotkey ⌥S
    func registerHotKey() {
        var eventType = EventTypeSpec(eventClass: OSType(kEventClassKeyboard),
                                      eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { _, _, userData -> OSStatus in
            let me = Unmanaged<AppDelegate>.fromOpaque(userData!).takeUnretainedValue()
            DispatchQueue.main.async { me.toggle() }
            return noErr
        }, 1, &eventType, Unmanaged.passUnretained(self).toOpaque(), nil)
        let hotKeyID = EventHotKeyID(signature: OSType(0x53485249), id: 1) // "SHRI"
        let status = RegisterEventHotKey(UInt32(kVK_ANSI_S), UInt32(optionKey), hotKeyID,
                                         GetApplicationEventTarget(), 0, &hotKeyRef)
        if status != noErr {
            // ⌥S already taken elsewhere — keep a Dock icon so the app stays reachable
            NSApp.setActivationPolicy(.regular)
        }
    }

    func applicationShouldHandleReopen(_ app: NSApplication,
                                       hasVisibleWindows: Bool) -> Bool {
        showPanel()
        return true
    }

    func applicationWillTerminate(_ note: Notification) {
        server?.terminate()
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
