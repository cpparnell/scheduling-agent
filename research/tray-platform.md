# Tray platform: rumps vs PyObjC-direct vs native SwiftUI over a Python daemon

Research for issue #24 (map: #20). Researched 2026-11-05 on macOS 14.4.1 (Sonoma), the dev machine.
Sources are primary (Apple developer docs and DTS forum answers, rumps/py2app source, `man launchd.plist`)
unless marked otherwise. Statements marked **[inference]** are my reasoning, not something a source says.

## TL;DR

**Build a native SwiftUI `MenuBarExtra` app (signed `.app` bundle) as the UI and the owner of macOS
permissions. Keep the Python core as a headless daemon that the app launches and talks to over a local
socket.** Two reasons settle it. Actionable notifications (buttons plus inline text reply) need
`UNUserNotificationCenter`, which needs an app bundle. TCC (Full Disk Access) attributes access to the
*responsible code*, which should be one stable signed app, not a Homebrew Python binary. rumps is
fine for the existing draft's scope (status, pause, recent list, osascript banners). It is a dead end
for the review-queue UI and for shipping. PyObjC-direct plus py2app could technically do everything,
but you would be fighting py2app signing and notarization on a 2.0 rewrite with an empty code-signing
docs section, all to avoid writing about 500 lines of Swift.

## Criteria matrix

| Criterion | rumps (venv, no bundle) | PyObjC-direct + py2app `.app` | SwiftUI app + Python daemon |
|---|---|---|---|
| Banner, no bundle | Only via `osascript` (attributed to Script Editor/osascript; no actions) | n/a (has bundle) | n/a (has bundle) |
| Banner, with bundle | `rumps.notification` (deprecated `NSUserNotification`) | `UNUserNotificationCenter` via `import UserNotifications` | `UNUserNotificationCenter` (first-class) |
| Actionable notifications (buttons, text reply) | Only through deprecated NSUserNotification, and only with a bundle | Yes (UN categories plus `UNTextInputNotificationAction`) | Yes |
| Scrollable queue, per-item actions | NSMenu items plus submenus only; no custom row views | Yes, hand-written AppKit (NSPopover/NSTableView) in Python | Yes: `MenuBarExtra(.window)` + `List` |
| Short free-text input | Modal `rumps.Window` (NSAlert + text field); `TextFieldMenuItem` exists on master only, unreleased | Yes | Yes: `TextField` |
| Date picker | None. Drop to raw PyObjC `NSDatePicker` | Yes (`NSDatePicker`) | Yes: SwiftUI `DatePicker` (macOS 10.15+) |
| FDA for chat.db: who needs it | The real Python binary (here `/opt/homebrew/Cellar/python@3.13/3.13.0_1/.../python3.13`) | The `.app` (if the bundle is the responsible code) | The `.app`, if it launches the daemon as a child or registers it via `SMAppService.agent` |
| Launch at login | Hand-rolled `~/Library/LaunchAgents` plist | `SMAppService` via PyObjC `ServiceManagement`, or legacy plist | `SMAppService.mainApp` (macOS 13+) |
| Ship to others | Not realistically: users need venv, Homebrew Python, an FDA grant on an interpreter | Possible but painful: py2app codesign/notarization is under-documented | Standard Xcode Developer ID + notarytool. The embedded Python must also be signed |

## Findings

### 1. Notifications

- rumps' `notification()` is built on `NSUserNotification`/`NSUserNotificationCenter`
  ([rumps/notifications.py](https://github.com/jaredks/rumps/blob/master/rumps/notifications.py)).
  When `defaultUserNotificationCenter()` returns `nil` it raises `RuntimeError` saying the
  "Info.plist file cannot be found or is missing CFBundleIdentifier". It looks for `Info.plist` next to
  `sys.executable`, which a venv `bin/` never has. This confirms the draft's finding.
- Apple deprecated `NSUserNotification` in macOS 11.0: "All NSUserNotifications API should be replaced
  with UserNotifications.frameworks API"
  ([NSUserNotification](https://developer.apple.com/documentation/foundation/nsusernotification)).
  rumps does expose action/other buttons and `has_reply_button`, but only on this deprecated API.
- `UNUserNotificationCenter` (macOS 10.14+) is "the central object for managing notification-related
  activities for your app or app extension". It supports categories and actions
  ([UNUserNotificationCenter](https://developer.apple.com/documentation/usernotifications/unusernotificationcenter)).
  `UNTextInputNotificationAction` gives inline typed replies: "the system displays controls for the
  user to enter or dictate the text content"
  ([UNTextInputNotificationAction](https://developer.apple.com/documentation/usernotifications/untextinputnotificationaction)).
  When the user picks an action, "the system launches your app in the background and notifies the
  shared UNUserNotificationCenter object, which notifies its delegate"
  ([Declaring actionable notification types](https://developer.apple.com/documentation/usernotifications/declaring-your-actionable-notification-types)).
  So the thing posting notifications must be a launchable app.
- From an unbundled process, `UNUserNotificationCenter.current()` dies with
  `NSInternalInconsistencyException: bundleProxyForCurrentProcess is nil`. Developers report this in
  [forum 724249](https://developer.apple.com/forums/thread/724249) (CLI) and
  [forum 679326](https://developer.apple.com/forums/thread/679326) (LaunchAgent). Neither thread has a
  DTS answer. The LaunchAgent poster's workaround is the architecture recommended here: a small
  menu-bar app posts the notifications and talks to the agent over IPC. Apple doesn't document the
  bundle requirement in so many words. Treat it as established by repeated reports plus the API's
  "your app" framing.
- `osascript display notification` works without a bundle. It has only title, subtitle, and sound,
  with no custom actions, and "the script or Script Editor … is added to the list of notifying apps"
  ([Mac Automation Scripting Guide: Displaying Notifications](https://developer.apple.com/library/archive/documentation/LanguagesUtilities/Conceptual/MacAutomationScriptingGuide/DisplayNotifications.html)).
  It's fine for "event created" FYI banners. It can't do "Confirm / Wrong day / Not a plan" buttons.
- PyObjC wraps the framework (`import UserNotifications`, new in 10.14). Its API notes list no
  restrictions ([PyObjC UserNotifications](https://pyobjc.readthedocs.io/en/latest/apinotes/UserNotifications.html)).
  **[inference]** The bundle requirement still applies, because it's a property of the process, not
  the language. So PyObjC-direct needs py2app (or a hand-built bundle) to get actionable notifications.
- All UN notifications need user authorization first (`requestAuthorization`), and a provisional
  option delivers quietly to Notification Center
  ([Asking permission](https://developer.apple.com/documentation/usernotifications/asking-permission-to-use-notifications)).

### 2. Queue UI, text input, date picker

- SwiftUI `MenuBarExtra` (macOS 13+) has a `.window` style that "renders its contents in a
  popover-like window" with normal controls. Menu-bar-only apps set `LSUIElement`
  ([MenuBarExtra](https://developer.apple.com/documentation/swiftui/menubarextra),
  [.window](https://developer.apple.com/documentation/swiftui/menubarextrastyle/window)).
  A scrollable `List` of pending items with per-row buttons, a `TextField`, and a `DatePicker`
  ("a control for selecting an absolute date", macOS 10.15+,
  [DatePicker](https://developer.apple.com/documentation/swiftui/datepicker)) are all stock components.
- rumps exposes only NSMenu-based UI: `MenuItem`, `SliderMenuItem`, `SeparatorMenuItem`, a modal
  `Window` built on `NSAlert` with a text field, and `alert()`
  ([rumps.py](https://github.com/jaredks/rumps/blob/master/rumps/rumps.py)). `TextFieldMenuItem`
  was merged to master on 2026-02-27 (PR #226), but the latest PyPI release is still **0.4.0 from
  2022-10-15**. There is no date picker and no custom row views. A per-item queue would be nested
  submenus, which is clumsy for "edit date, then confirm". Anything beyond that means dropping into
  raw PyObjC anyway.
- **[inference]** PyObjC-direct can build all of it (NSStatusItem + NSPopover + NSTableView +
  NSDatePicker). But it's imperative AppKit with no Interface Builder or SwiftUI previews, and much
  more code than the SwiftUI equivalent.

### 3. Full Disk Access (chat.db): which process needs it

- Apple DTS (Quinn) says you must grant FDA to the *responsible code*. If a process is blocked, "the
  responsible code will show up in System Settings > Privacy & Security > Full Disk Access even when
  no alert is presented" ([forum 760291](https://developer.apple.com/forums/thread/760291)).
- "TCC tracks access via code signing constructs, and those are properties of the *executable* …
  when you run a shell script the process is running `sh` … and that's all that TCC knows about it"
  ([forum 760291](https://developer.apple.com/forums/thread/760291)). "Scripting and TCC are uneasy
  bedfellows" ([forum 745692](https://developer.apple.com/forums/thread/745692)).
  **[inference]** Today's grant goes to the Homebrew `python3.13` binary, and every script run by that
  interpreter inherits it. A `brew upgrade` moves the Cellar path (the venv resolves to
  `/opt/homebrew/Cellar/python@3.13/3.13.0_1/...`) and will likely invalidate the grant.
- "If your agent were a helper tool embedded in an app, TCC would recognise that and treat the app
  as the responsible code" ([forum 694948](https://developer.apple.com/forums/thread/694948)). From
  [On File System Permissions (678819)](https://developer.apple.com/forums/thread/678819): the
  decision should be "recorded for the whole app, not that specific helper tool". The heuristics
  work "reasonably well" but break when "the child process does something 'odd' … such as trying to
  daemonise itself". For launchd jobs that aren't attributed correctly, add `AssociatedBundleIdentifiers`.
- `man launchd.plist`: `AssociatedBundleIdentifiers` "indicates which bundles are associated with
  this job in the System Settings Login Items UI. If an app installs a legacy plist the plist should
  include this key". Caveats: "Daemons and agents managed by launchd are subject to macOS user
  privacy protections."
- Per option:
  - **rumps:** FDA goes on the real Python executable (not the venv symlink). It covers every Python
    script on the machine and is brittle across interpreter upgrades.
  - **SwiftUI + daemon:** FDA goes on the `.app`. That requires the Python daemon to be attributed to
    it, either as a child process the app spawns (don't double-fork or daemonize) or as an
    `SMAppService.agent` whose plist lives inside the bundle. **[inference]** The interpreter has to
    live inside the bundle (and be signed with it) for attribution to be robust. A venv Python
    launched by the app will probably still attribute to the app as responsible process, but TCC's
    algorithm is "undocumented and complex" (forum 760291). **Spike this before committing.**
  - Alternative worth noting: have the Swift app open `chat.db` itself and stream rows to Python.
    Only the app ever touches the protected file, and attribution is unambiguous. It costs moving the
    reader (the SQL + `attributedBody` decoding) to Swift.

### 4. Launch at login

- `SMAppService` (macOS 13+) registers `LoginItems`, `LaunchAgents` and `LaunchDaemons` "as helper
  executables for your app". `register()` is the "replacement for installing property lists in
  `~/Library/LaunchAgents`" ([SMAppService](https://developer.apple.com/documentation/servicemanagement/smappservice)).
  `SMAppService.mainApp` makes the app itself a login item
  ([mainApp](https://developer.apple.com/documentation/servicemanagement/smappservice/mainapp)).
  `agent(plistName:)` requires the plist in "the calling app's `Contents/Library/LaunchAgents`
  directory" ([agent(plistName:)](https://developer.apple.com/documentation/servicemanagement/smappservice/agent(plistname:))).
- Apple's sample says the bundled-plist approach keeps plists "in a fully codesigned app bundle that
  neither the system nor a third party can modify". Users "can also see which app is providing the
  launch daemons or launch agents" in System Settings > General > Login Items
  ([Updating your app package installer…](https://developer.apple.com/documentation/servicemanagement/updating-your-app-package-installer-to-use-the-new-service-management-api)).
- The draft's hand-written `~/Library/LaunchAgents` plist with venv paths works for one developer.
  On macOS 13+ it shows up in Login Items as an unattributed item, and it breaks when the repo moves.

### 5. Shipping to other people

- Since macOS 10.15, "all software built after June 1, 2019, and distributed with Developer ID must be
  notarized". Requirements: every executable signed, a Developer ID certificate, Hardened Runtime, a
  secure timestamp, no `get-task-allow`, and `notarytool` (altool is gone since 2023-11-01)
  ([Notarizing macOS software](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution)).
- **rumps from a venv:** not shippable as-is. Users would need Python, the venv, a manual FDA grant
  on an interpreter, and a hand-installed LaunchAgent.
- **py2app:** the 2.0 rewrite dropped "semi standalone" mode because it "is also not compatible with
  code signing (Notarization)". Its pyproject docs have a "Code signing configuration" section that
  reads, verbatim, "This section is intentionally left blank". The changelog notes
  "#344: Invocation of codesign on the whole bundle sometimes fails … at best a workaround"
  ([py2app changelog](https://github.com/ronaldoussoren/py2app/blob/main/doc/changelog.rst),
  [pyproject.rst](https://github.com/ronaldoussoren/py2app/blob/main/doc/pyproject.rst)).
  Latest PyPI release is 0.28.10 (2026-02-13). It's workable, but you're on your own.
- **SwiftUI + daemon:** the app side is the standard Xcode Archive → Developer ID → notarize flow.
  **[inference]** The hard part moves to embedding Python (for example a relocatable standalone
  CPython build plus site-packages in `Contents/Resources`). Every `.so`/`.dylib` has to be
  signed with Hardened Runtime. This is the same work py2app would do, but done once in a build script
  you control. Until shipping matters, the daemon can keep running from the dev venv.

## Recommendation

1. **Platform: native SwiftUI `MenuBarExtra(.window)` app + Python daemon.** The app owns the UI,
   `UNUserNotificationCenter` (actionable "Confirm / Wrong day / Not a plan" plus a text-reply action),
   `SMAppService.mainApp` for login, and the TCC identity. Python keeps detection, dedup, calendar,
   and state.
2. **IPC:** a local Unix-domain socket (or localhost HTTP) with a small JSON protocol: `queue.list`,
   `item.confirm`, `item.reject`, `item.set_date`, `item.note`, `status`, `pause`/`resume`, plus a push
   stream of `event.created`/`item.pending`. **[inference]** XPC is overkill while the daemon is
   Python. A socket keeps the headless `python main.py` path and tests untouched.
3. **Process model:** the app spawns the daemon as a child (no daemonizing) so FDA attributes to the
   app. Later, move to an `SMAppService.agent` plist in the bundle if the daemon must outlive the UI.
4. **De-risk first with a spike (about half a day):** a minimal signed SwiftUI menu-bar app that
   (a) spawns `venv/bin/python -c "open chat.db"` with FDA granted only to the app, and
   (b) posts a UN notification with a text-input action and receives the reply. If (a) fails,
   fall back to the Swift app reading `chat.db` and streaming rows to Python.
5. **Don't build the rumps draft as the long-term tray.** At most it's a stopgap. The draft's
   `hooks.py` / `AgentRunner` / `get_recent_created` seams are still the right Python-side work,
   because they become the daemon's IPC surface.

## Open questions / not verified

- Whether a venv/Homebrew interpreter spawned by a signed app reliably attributes FDA to the app on
  macOS 14/15. TCC's responsible-process algorithm is undocumented, so test it in the spike.
- Whether Apple documents the bundle requirement for `UNUserNotificationCenter` anywhere explicit.
  The evidence here is repeated developer reports plus the API's app-centric framing.
- The user's minimum macOS. `MenuBarExtra` and `SMAppService` need macOS 13. The dev box is 14.4.1.
