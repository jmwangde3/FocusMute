# FocusMute

[English](#english) · [中文](#中文)

---

## English

A small Windows tray utility: **when a watched app goes to the background, its own audio is
muted and (optionally) its process is suspended; when you come back, both are restored.**
It touches exactly one app's audio session — everything else keeps working normally.

### What it does

- Watches the focused window. Any watched app that is **not** in focus gets **its own audio
  session muted**; if `pause` is on for it, the whole process is suspended too.
- Focus is judged **per process family**: a game and the launcher that started it count as one
  game, while shell processes (`explorer.exe`, `steam.exe`, `cmd.exe`, …) never count as the game.
- **Two separate delays**: `delay_ms` (mute, default **2000 ms**) and `pause_delay_ms` (suspend,
  default **0 ms**). A game that keeps running in the background can kill the player in those two
  seconds, so pausing is immediate by default; both values are configurable.
- If Windows refuses to report a foreground window (some exclusive-fullscreen games), it **holds
  the previous state** instead of guessing.
- The wanted mute state is **pushed back to the session every 2 seconds**; Windows remembers
  "this app was muted" per app, and a newly created session can otherwise start out muted.
- **Config is reloaded live**: save `focusmute.ini` and it applies about half a second later;
  the tray menu also has a manual reload entry.
- **Tray status rows are the way back from a suspended app.** A suspended window is a "hung
  window" for Windows: clicking it or its taskbar button does **not** activate it, so the tool
  never sees the focus change. Click the app's row in the tray menu instead — it resumes that one
  app, unmutes it and brings its window to the front. Only the app you clicked is touched.
- After a manual restore the app is **held**: it will not be auto-suspended again until it has
  been in the foreground once (otherwise it would be frozen again 0.25 s after the click).

### Requirements

- Windows 10 / 11
- From source: Python 3.10+, then `pip install -r requirements.txt`
- Or build/download `FocusMute.exe` — no Python needed.

### Install (from source)

```
pip install -r requirements.txt
pythonw focusmute.pyw
```

Auto-start: put a shortcut to `FocusMute.exe` (or `pythonw.exe focusmute.pyw`) into `shell:startup`.

### Build the exe

`build.cmd` runs PyInstaller and leaves `dist\FocusMute.exe` (single file, no console window).

### Configuration — `focusmute.ini`

| Key | Meaning |
|-----|---------|
| `process` | executable name(s) to watch, comma separated, case-insensitive |
| `mute` | mute the target's audio while it is in the background (default `true`) |
| `pause` | also suspend the target's process while it is in the background (default `false`) |
| `delay_ms` | how long the target must stay in the background before muting (default `2000`) |
| `pause_delay_ms` | how long before suspending (default `0` = next poll, about 0.25 s) |
| `poll_ms` | how often the focused window is checked (default `250`) |

Per-app overrides — add a `[target:<exe name>]` section and only the keys you change:

```ini
[general]
process = SomeGame.exe, AnotherGame.exe
pause = false
delay_ms = 2000
pause_delay_ms = 0

[target:SomeGame.exe]
pause = true
pause_delay_ms = 0
```

That means: both games are muted in the background, only `SomeGame` is suspended, immediately.
Edits apply on save — no restart needed.

### Tray menu

- Header `状态（点游戏名：恢复并切回）` — status rows follow.
- One row per configured target: `front` / `follows the front` / `paused` / `pause failed` /
  `resumed (held)` / `muted` / `no audio session` / `not running`. Refreshed every 2 seconds.
- **Click a running target's row** = resume it, unmute it, bring its window to the front.
  Rows of apps that are not running are greyed out.
- `Auto mute+pause: on` — switching it off **hands the sound and the suspended process back
  immediately**. Left-clicking the icon is the same action.
- `Reload config` / `Open log` / `Edit config` / `Quit` (quitting releases everything).

### Limitations

- A suspended window cannot be activated by clicking it (Windows treats it as hung); use the
  tray row.
- Only the target's **own** audio session is touched; other apps are unaffected.
- If an app uses **WASAPI exclusive** output, per-app muting cannot reach it.
- `pause = true` suspends the whole process: risky for apps that write saves or talk to a server.
- The tray menu, status labels and log lines are in Chinese.

### License

MIT — see [LICENSE](LICENSE).

---

## 中文

一个 Windows 托盘小工具：**被监视的程序切到后台就静音它自己的声音，需要时再把整个进程挂起；
切回前台两者都恢复。** 它只动目标程序自己的音频会话，不影响其他软件。

### 行为要点

- 看前台窗口；不在前台的目标，把**它自己的音频会话**静音；该目标开了 `pause` 时整进程一起挂起。
- 前台判定按**进程家族**算：游戏和启动它的启动器算同一个游戏；`explorer.exe`、`steam.exe`、
  `cmd.exe` 这类壳不会算成游戏。
- **静音和暂停的延迟分开**：`delay_ms`（静音，默认 **2000 毫秒**）和 `pause_delay_ms`
  （挂起，默认 **0**）。切出去还在跑的游戏，那两秒足够把人打死，所以暂停默认立即生效；
  两个值都能改。
- 拿不到前台窗口时（某些全屏独占游戏）**保持原状态**，不猜。
- 每 2 秒把静音状态**硬写回会话**一次：Windows 会按程序记住「被静音过」，新会话可能一出生就是静的。
- **配置保存即生效**：改完 `focusmute.ini` 约半秒自动重载，托盘菜单里也有「重载配置」。
- **挂起后的窗口只能从托盘那一行叫回来。** 被挂起的窗口对 Windows 来说是"无响应窗口"：
  点它、点它任务栏按钮都**不会**把前台切过去，工具看不到焦点变化就不会恢复。点托盘菜单里
  该游戏那一行——只恢复这一个、解除静音、把窗口切回前台，其他游戏不受影响。
- 手动恢复过的程序进入**已放行**：在回到前台一次之前不会再被自动挂起（否则点完 0.25 秒又冻住）。

### 需要

- Windows 10 / 11
- 源码运行：Python 3.10+，先 `pip install -r requirements.txt`
- 或者直接构建/下载 `FocusMute.exe`，不需要装 Python。

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
| `process` | 要监视的程序名，逗号分隔，大小写随意 |
| `mute` | 切后台时静音目标自己的音频（默认 `true`） |
| `pause` | 切后台时顺便挂起目标进程（默认 `false`） |
| `delay_ms` | 后台停留多久才静音（默认 `2000`） |
| `pause_delay_ms` | 后台停留多久才挂起（默认 `0`，即下次轮询、约 0.25 秒） |
| `poll_ms` | 检测间隔毫秒（默认 `250`） |

按程序单独设置：加一个 `[target:<exe 名>]` 节，只写要改的键：

```ini
[general]
process = SomeGame.exe, AnotherGame.exe
pause = false
delay_ms = 2000
pause_delay_ms = 0

[target:SomeGame.exe]
pause = true
pause_delay_ms = 0
```

意思是：两个游戏切后台都静音，只有 `SomeGame` 会被挂起，而且是立即挂起。
保存即生效，不用重启。

### 托盘

- 第一行 `状态（点游戏名：恢复并切回）`，下面每个目标一行：`前台` / `随前台` / `暂停` /
  `暂停失败` / `已放行` / `静音` / `无音频会话` / `未运行`，每 2 秒刷新
- **点在运行的目标那一行** = 恢复它、解除静音、把窗口切回前台；没在运行的行是灰的
- `自动静音+暂停：已开启` 这一行关掉自动行为时**立刻把声音和挂起的进程还给你**；
  左键点图标等同这一行
- `重载配置` / `打开日志` / `编辑配置` / `退出`（退出会解除一切）

### 已知限制

- 挂起后的窗口**不能被点击激活**（Windows 当它是无响应窗口），只能走托盘那一行
- 只动目标自己的音频会话，其他软件不受影响
- 目标若走 **WASAPI 独占**输出，按程序静音管不住
- `pause = true` 是挂起整个进程：正在写存档或联网的游戏慎用
- 界面、状态文字和日志是中文

### 许可

MIT，见 [LICENSE](LICENSE)。
