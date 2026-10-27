# FocusMute

[English](#english) · [中文](#中文)

---

## English

A small Windows tray utility: **when a watched app goes to the background, its own audio is
muted; when you come back, the sound returns.** Made for games that keep playing music while
you are in another window. It mutes exactly one app's audio session — everything else keeps
working normally.

### What it does

- Watches the focused window. Any watched app that is **not** in focus gets **its own audio
  session muted** (optionally its process is suspended as well).
- Focus is judged **per process family**: a game and the launcher that started it count as one
  game, while shell processes (`explorer.exe`, `steam.exe`, `cmd.exe`, …) never count as the game.
- Muting only happens after the app has been in the background for **2 seconds**, so the focus
  blink of a fullscreen switch does not cut anything. Restoring the sound is immediate.
- If Windows refuses to report a foreground window (happens with some exclusive-fullscreen
  games), it **holds the previous state** instead of guessing.
- The wanted state is **pushed back to the session every 2 seconds**. Windows remembers "this
  app was muted" per app; a newly created audio session can otherwise start out muted.
- Multiple targets are supported: **only the app in focus is audible**, the rest stay muted.

### Requirements

- Windows 10 / 11
- From source: Python 3.10+, then `pip install -r requirements.txt`
- Or download `FocusMute.exe` from Releases — no Python needed.

### Install (from source)

```
pip install -r requirements.txt
pythonw focusmute.pyw
```

Auto-start: create a shortcut to `FocusMute.exe` (or to `pythonw.exe focusmute.pyw`) and put it
into `shell:startup`.

### Build the exe

`build.cmd` runs PyInstaller and leaves `dist\FocusMute.exe` (single file, no console window).

### Configuration — `focusmute.ini`

| Key | Meaning |
|-----|---------|
| `process` | executable name(s) to watch, comma separated, case-insensitive. Several entries are fine. |
| `mute` | mute the target's audio while it is in the background (default `true`) |
| `pause` | also suspend the target's process while it is in the background (default `false`) |
| `poll_ms` | how often the focused window is checked (default `250`) |

Per-app overrides — add a `[target:<exe name>]` section and only the keys you change:

```ini
[general]
process = FFRS-Win64-Shipping.exe, vwport.exe
pause = false

[target:vwport.exe]
pause = true
```

Restart the app from the tray menu after editing.

### Tray menu

- Hover the icon for the per-target status line.
- One grey row per configured target: `front` / `follows the front` / `muted` / `paused` /
  `no audio session` / `not running`. Refreshed every 2 seconds.
- The toggle row switches the automatic behaviour off **and hands the sound back immediately**.
  Left-clicking the icon is the same action.
- `Open log`, `Edit config`, `Quit` (quitting releases everything).

### Limitations

- Only the target's **own** audio session is touched; other apps are unaffected.
- If an app uses **WASAPI exclusive** output, per-app muting cannot reach it.
- `pause = true` suspends the whole process: risky for games that write saves or talk to a
  server.
- The tray menu, status labels and log lines are in Chinese.

### License

MIT — see [LICENSE](LICENSE).

---

## 中文

一个 Windows 托盘小工具：**被监视的程序切到后台就静音它自己的声音，回来立刻恢复。**
给那种「切出去还在放 BGM」的游戏用。它只动目标程序自己的音频会话，不影响其他软件。

### 行为要点

- 看前台窗口；不在前台的目标，把**它自己的音频会话**静音（配置里可以让它顺便把进程挂起）。
- 前台判定按**进程家族**算：游戏和启动它的启动器算同一个游戏；`explorer.exe`、`steam.exe`、
  `cmd.exe` 这类壳不会算成游戏。
- 后台要**持续 2 秒**才静音，全屏切换那一瞬间的焦点闪烁不会切声音；恢复声音是立刻的。
- 拿不到前台窗口时（某些全屏独占游戏会这样）**保持原状态**，不猜。
- 每 2 秒把认定状态**硬写回会话**一次：Windows 会按程序记住「被静音过」，新会话可能一出生就是静的。
- 支持多个目标：**只有前台那个出声**，其余保持静音。

### 需要

- Windows 10 / 11
- 源码运行：Python 3.10+，先 `pip install -r requirements.txt`
- 或者直接下 Releases 里的 `FocusMute.exe`，不需要装 Python。

### 安装（源码）

```
pip install -r requirements.txt
pythonw focusmute.pyw
```

开机自启：给 `FocusMute.exe`（或 `pythonw.exe focusmute.pyw`）做个快捷方式放进 `shell:startup`。

### 打包 exe

`build.cmd` 跑 PyInstaller，产物在 `dist\FocusMute.exe`（单文件、无控制台窗口）。

### 配置 `focusmute.ini`

| 键 | 说明 |
|----|------|
| `process` | 要监视的程序名，逗号分隔，大小写随意；可以写多个 |
| `mute` | 切后台时静音目标自己的音频（默认 `true`） |
| `pause` | 切后台时顺便挂起目标进程（默认 `false`） |
| `poll_ms` | 检测间隔毫秒（默认 `250`） |

按程序单独设置：加一个 `[target:<exe 名>]` 节，只写要改的键，见上面的英文示例。改完从托盘退出再启动。

### 托盘

- 悬停看逐目标状态行
- 每个目标一行灰字：`前台` / `随前台` / `静音` / `暂停` / `无音频会话` / `未运行`，每 2 秒刷新
- 开关那一行关掉自动行为时**立刻把声音还给你**；左键点图标等同这一行
- `打开日志`、`编辑配置`、`退出`（退出会解除一切）

### 已知限制

- 只动目标自己的音频会话，其他软件不受影响
- 目标若走 **WASAPI 独占**输出，按程序静音管不住
- `pause = true` 是挂起整个进程：正在写存档或联网的游戏慎用
- 界面、状态文字和日志是中文

### 许可

MIT，见 [LICENSE](LICENSE)。
