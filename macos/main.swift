// share-it — native macOS shell: floating vibrancy panel + global hotkey (⌥S)
// wrapping the local Python backend's web UI.
import AppKit
import WebKit
import Carbon.HIToolbox
import Quartz

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

    func pythonPath() -> String {
        // prefer the bundled standalone CPython (DMG); fall back to system python3 (dev)
        if let res = Bundle.main.resourcePath {
            let embedded = res + "/python/bin/python3"
            if FileManager.default.isExecutableFile(atPath: embedded) { return embedded }
        }
        return "/usr/bin/env"
    }

    func startServer() {
        let probe = URL(string: "http://127.0.0.1:\(port)/api/shares")!
        if (try? Data(contentsOf: probe)) != nil { return } // already running (dev)
        let p = Process()
        let py = pythonPath()
        if py == "/usr/bin/env" {
            p.executableURL = URL(fileURLWithPath: "/usr/bin/env")
            p.arguments = ["python3", backendDir() + "/app.py", "--no-browser"]
        } else {
            p.executableURL = URL(fileURLWithPath: py)
            p.arguments = [backendDir() + "/app.py", "--no-browser"]
        }
        try? p.run()
        server = p
    }

    func waitForServer(attempts: Int) {
        let health = URL(string: "http://127.0.0.1:\(port)/api/health")!
        func poll(_ left: Int) {
            var req = URLRequest(url: health); req.timeoutInterval = 0.5
            URLSession.shared.dataTask(with: req) { data, resp, _ in
                let ok = (resp as? HTTPURLResponse)?.statusCode == 200
                    && data.map { String(decoding: $0, as: UTF8.self).contains("share-it") } == true
                DispatchQueue.main.async {
                    if ok {
                        self.webView.load(URLRequest(url: URL(string: "http://127.0.0.1:\(self.port)/")!))
                        self.showPanel()
                    } else if left > 0 {
                        DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { poll(left - 1) }
                    } else {
                        self.startupFailed()
                    }
                }
            }.resume()
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
        let size = NSSize(width: 700, height: 540)
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
        effect.material = .hudWindow
        effect.blendingMode = .behindWindow
        effect.state = .active
        effect.wantsLayer = true
        effect.layer?.cornerRadius = 18
        effect.layer?.masksToBounds = true
        effect.autoresizingMask = [.width, .height]

        let cfg = WKWebViewConfiguration()
        cfg.userContentController.add(self, name: "shareit")
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

    func windowDidResignKey(_ notification: Notification) {
        // don't vanish beneath our own Quick Look panel
        if QLPreviewPanel.sharedPreviewPanelExists(),
           QLPreviewPanel.shared().isVisible { return }
        panel.orderOut(nil)
    }

    // MARK: JS bridge
    func userContentController(_ ucc: WKUserContentController, didReceive message: WKScriptMessage) {
        if message.body as? String == "hide" { panel.orderOut(nil); return }
        guard let dict = message.body as? [String: Any],
              let cmd = dict["cmd"] as? String else { return }
        let files = (dict["files"] as? [String] ?? []).map { URL(fileURLWithPath: $0) }
        switch cmd {
        case "copyFiles":  // real file objects on the pasteboard — Finder-grade paste
            let pb = NSPasteboard.general
            pb.clearContents()
            pb.writeObjects(files as [NSPasteboardWriting])
        case "quickLook":
            qlFiles = files
            if let ql = QLPreviewPanel.shared() {
                ql.dataSource = self
                ql.delegate = self
                ql.makeKeyAndOrderFront(nil)
                ql.reloadData()
                NotificationCenter.default.addObserver(
                    forName: NSWindow.willCloseNotification, object: ql, queue: .main) { [weak self] _ in
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
