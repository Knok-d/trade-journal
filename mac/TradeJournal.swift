// Нативная оболочка дневника: окно с WKWebView на локальный сервер.
//
// Интерфейс остаётся тем же HTML — оболочка не рисует ничего своего и не знает
// ни про сделки, ни про биржу. Она нужна ровно за тем, чего веб-приложение
// Safari не даёт: собственный бандл в /Applications, своё меню, своя иконка и
// независимость от того, чем именно macOS решит открывать страницы.
//
// Сервер поднимает LaunchAgent (см. deploy/README.md), поэтому оболочка его не
// запускает: две сущности, управляющие одним процессом, рано или поздно
// разойдутся. Если сервера нет, окно честно говорит об этом и продолжает
// стучаться — после логина агент стартует не мгновенно.

import AppKit
import WebKit

let defaultURL = "http://127.0.0.1:8321/"
let retryInterval: TimeInterval = 2
let retryLimit = 30

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate {
    private var window: NSWindow!
    private var web: WKWebView!
    private var status: NSTextField!
    private var attempts = 0

    private var url: URL {
        URL(string: ProcessInfo.processInfo.environment["TRADE_JOURNAL_URL"] ?? defaultURL)!
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()
        buildWindow()
        load()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    // MARK: окно

    private func buildWindow() {
        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1280, height: 860),
            styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
            backing: .buffered, defer: false)
        window.title = "Trade Journal"
        window.titlebarAppearsTransparent = true
        window.minSize = NSSize(width: 720, height: 480)
        // Тема окна фиксированная: страница нарисована под тёмный фон, и на
        // светлой системе белые поля вокруг тёмной вёрстки выглядят поломкой.
        window.appearance = NSAppearance(named: .darkAqua)
        window.backgroundColor = NSColor(red: 0.008, green: 0.023, blue: 0.090, alpha: 1)
        // Положение и размер переживают перезапуск: дневник открывают каждый
        // день, и каждый день двигать окно — ровно то трение, из-за которого
        // инструментом перестают пользоваться.
        window.setFrameAutosaveName("TradeJournalWindow")

        let config = WKWebViewConfiguration()
        web = WKWebView(frame: .zero, configuration: config)
        web.navigationDelegate = self
        web.setValue(false, forKey: "drawsBackground")
        web.autoresizingMask = [.width, .height]
        window.contentView = web

        status = NSTextField(labelWithString: "")
        status.font = .systemFont(ofSize: 13)
        status.textColor = .secondaryLabelColor
        status.alignment = .center
        status.isHidden = true
        status.translatesAutoresizingMaskIntoConstraints = false
        web.addSubview(status)
        NSLayoutConstraint.activate([
            status.centerXAnchor.constraint(equalTo: web.centerXAnchor),
            status.centerYAnchor.constraint(equalTo: web.centerYAnchor),
            status.widthAnchor.constraint(lessThanOrEqualTo: web.widthAnchor, constant: -80),
        ])

        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    // MARK: загрузка

    @objc func load() {
        status.isHidden = true
        web.load(URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData))
    }

    @objc func reload() {
        attempts = 0
        load()
    }

    private func failed(_ error: Error) {
        attempts += 1
        guard attempts <= retryLimit else {
            show("Дневник не отвечает на \(url.absoluteString).\n" +
                 "Проверь агент: launchctl list | grep trade-journal")
            return
        }
        show("Жду дневник на \(url.absoluteString)…")
        // Агент после логина стартует не мгновенно, поэтому первые неудачи —
        // норма, а не повод показывать ошибку.
        perform(#selector(load), with: nil, afterDelay: retryInterval)
    }

    private func show(_ text: String) {
        status.stringValue = text
        status.isHidden = false
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        attempts = 0
        status.isHidden = true
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!,
                 withError error: Error) {
        failed(error)
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!,
                 withError error: Error) {
        failed(error)
    }

    /// Ссылки наружу открываются в браузере, а не подменяют собой дневник:
    /// окно приложения должно всегда показывать дневник и ничего больше.
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let target = action.request.url else {
            decisionHandler(.cancel)
            return
        }
        if target.host == url.host && target.port == url.port {
            decisionHandler(.allow)
        } else {
            NSWorkspace.shared.open(target)
            decisionHandler(.cancel)
        }
    }

    // MARK: меню

    private func buildMenu() {
        let main = NSMenu()

        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "О программе Trade Journal",
                        action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Скрыть", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(withTitle: "Завершить", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        main.addItem(appItem)

        let viewItem = NSMenuItem()
        let viewMenu = NSMenu(title: "Вид")
        let refresh = NSMenuItem(title: "Обновить", action: #selector(reload), keyEquivalent: "r")
        refresh.target = self
        viewMenu.addItem(refresh)
        viewMenu.addItem(withTitle: "Во весь экран",
                         action: #selector(NSWindow.toggleFullScreen(_:)), keyEquivalent: "f")
        viewItem.submenu = viewMenu
        main.addItem(viewItem)

        let windowItem = NSMenuItem()
        let windowMenu = NSMenu(title: "Окно")
        windowMenu.addItem(withTitle: "Свернуть",
                           action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        windowMenu.addItem(withTitle: "Закрыть",
                           action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        windowItem.submenu = windowMenu
        main.addItem(windowItem)

        NSApp.mainMenu = main
        NSApp.windowsMenu = windowMenu
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
