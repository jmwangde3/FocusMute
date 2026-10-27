"""FocusMute - 目标程序切到后台就静音（可选暂停），切回来恢复。

托盘图标看状态，右键有菜单。配置在同目录 focusmute.ini，可填多个程序名。
日志 focusmute.log，崩溃/强杀后下次启动会自动恢复被暂停的进程（focusmute.state.json）。

用法：
    pythonw focusmute.pyw              正常跑（托盘）
    python  focusmute.pyw --selftest   自检：配置、Win32 接口、音频会话能不能看到
    python  focusmute.pyw --no-tray    无托盘跑（控制台看日志，调参用）
"""
import ctypes
import ctypes.wintypes as wt
import configparser
import json
import os
import sys
import threading
import time

if getattr(sys, "frozen", False):
    # PyInstaller one-file build: __file__ lives in a temp extraction dir, the config
    # and logs must stay next to the exe
    BASE = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
CONF_PATH = os.path.join(BASE, "focusmute.ini")
LOG_PATH = os.path.join(BASE, "focusmute.log")
STATE_PATH = os.path.join(BASE, "focusmute.state.json")
LOG_MAX = 512 * 1024

PROCESS_SUSPEND_RESUME = 0x0800
MUTE_DELAY_S = 2.0      # the target must stay in the background this long before muting
REASSERT_S = 2.0        # re-push the wanted mute state this often (see apply())
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll")

user32.GetForegroundWindow.restype = wt.HWND
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.CloseHandle.argtypes = [wt.HANDLE]
ntdll.NtSuspendProcess.argtypes = [wt.HANDLE]
ntdll.NtSuspendProcess.restype = ctypes.c_long
ntdll.NtResumeProcess.argtypes = [wt.HANDLE]
ntdll.NtResumeProcess.restype = ctypes.c_long


# --------------------------------------------------------------------------- log
def log(msg):
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX:
            os.replace(LOG_PATH, LOG_PATH + ".1")
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


# ------------------------------------------------------------------------ config
class Config(object):
    def __init__(self, path=CONF_PATH):
        self.processes = []
        self.poll_ms = 250
        self.do_mute = True
        self.do_pause = False
        parser = configparser.ConfigParser()
        parser.read(path, encoding="utf-8")
        section = parser["general"] if parser.has_section("general") else {}
        raw = section.get("process", "")
        self.processes = [p.strip().lower() for p in raw.replace(";", ",").split(",") if p.strip()]
        try:
            self.poll_ms = max(50, int(section.get("poll_ms", "250")))
        except ValueError:
            self.poll_ms = 250
        self.do_mute = section.get("mute", "true").strip().lower() in ("1", "true", "yes", "on")
        self.do_pause = section.get("pause", "false").strip().lower() in ("1", "true", "yes", "on")

        # every process starts with the global switches, then [target:<exe>]
        # sections override them one by one
        self.targets = {}
        for name in self.processes:
            self.targets[name] = {"mute": self.do_mute, "pause": self.do_pause}
        for sec in parser.sections():
            if not sec.lower().startswith("target:"):
                continue
            name = sec.split(":", 1)[1].strip().lower()
            if name not in self.targets:
                log("配置项 [%s] 不在 process 列表里，忽略" % sec)
                continue
            body = parser[sec]
            for key in ("mute", "pause"):
                raw_value = body.get(key)
                if raw_value is not None:
                    self.targets[name][key] = raw_value.strip().lower() in ("1", "true", "yes", "on")

    def flags(self, name):
        return self.targets.get(name, {"mute": self.do_mute, "pause": self.do_pause})


# ---------------------------------------------------------------------- win32 bits
user32.GetWindowTextW.argtypes = [wt.HWND, ctypes.c_void_p, ctypes.c_int]
user32.GetWindowTextLengthW.argtypes = [wt.HWND]


def foreground_info():
    """(pid, window title). pid 0 means Windows would not tell us - fullscreen
    exclusive games sometimes leave GetForegroundWindow returning NULL."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return 0, ""
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return pid.value, buf.value


def foreground_pid():
    return foreground_info()[0]


def process_alive(pid):
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    kernel32.CloseHandle(handle)
    return True


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD),
                ("cntUsage", wt.DWORD),
                ("th32ProcessID", wt.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_size_t)),
                ("th32ModuleID", wt.DWORD),
                ("cntThreads", wt.DWORD),
                ("th32ParentProcessID", wt.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wt.DWORD),
                ("szExeFile", wt.WCHAR * 260)]


kernel32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wt.HANDLE
kernel32.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32FirstW.restype = wt.BOOL
kernel32.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32NextW.restype = wt.BOOL

TH32CS_SNAPPROCESS = 0x00000002


def snapshot(names):
    """one process snapshot -> (matched pids per exe, child->parent map)"""
    wanted = set(n.lower() for n in names)
    found, parents = {}, {}
    handle = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if handle == wt.HANDLE(-1).value or not handle:
        return found, parents
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        more = kernel32.Process32FirstW(handle, ctypes.byref(entry))
        while more:
            exe = entry.szExeFile.lower()
            pid = int(entry.th32ProcessID)
            parents[pid] = int(entry.th32ParentProcessID)
            if exe in wanted:
                found.setdefault(exe, []).append(pid)
            more = kernel32.Process32NextW(handle, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(handle)
    return found, parents


def find_pids(names):
    return snapshot(names)[0]


SHELL_PARENT_NAMES = {
    # a game started from one of these must not inherit its "focus" from the shell
    "explorer.exe", "steam.exe", "steamwebhelper.exe", "epicgameslauncher.exe",
    "cmd.exe", "powershell.exe", "pwsh.exe", "conhost.exe", "wt.exe", "wsl.exe",
    "bash.exe", "python.exe", "pythonw.exe", "code.exe", "devenv.exe",
    "taskhostw.exe", "svchost.exe", "services.exe", "wininit.exe", "lsass.exe",
    "searchhost.exe", "shellexperiencehost.exe",
}


def exe_name_by_pid():
    out = {}
    handle = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if handle == wt.HANDLE(-1).value or not handle:
        return out
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        more = kernel32.Process32FirstW(handle, ctypes.byref(entry))
        while more:
            out[int(entry.th32ProcessID)] = entry.szExeFile.lower()
            more = kernel32.Process32NextW(handle, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(handle)
    return out


def family_map(names):
    """per target pid: itself plus every descendant. A launcher and the real game it
    starts are two exes and the focused window may belong to either, but two separate
    games must NOT be one family (the background one still gets muted)."""
    found, parents = snapshot(names)
    children = {}
    for child, parent in parents.items():
        children.setdefault(parent, []).append(child)
    families = {}
    exe_of = exe_name_by_pid()
    for pid in (p for sub in found.values() for p in sub):
        fam, stack = {pid}, [pid]
        while stack:
            current = stack.pop()
            for child in children.get(current, ()):
                if child not in fam:
                    fam.add(child)
                    stack.append(child)
        parent = parents.get(pid)
        if parent and exe_of.get(parent) and exe_of[parent] not in SHELL_PARENT_NAMES:
            # the launcher window counts as this game: he keeps only the real exe listed
            fam.add(parent)
        families[pid] = fam
    # two configured names that are ancestor/descendant of each other (launcher + the
    # game it starts) are one game: focusing either must keep both audible
    changed = True
    while changed:
        changed = False
        for a in list(families):
            for b in list(families):
                if a == b:
                    continue
                if b in families[a] or a in families[b]:
                    union = families[a] | families[b]
                    if union != families[a] or union != families[b]:
                        families[a], families[b] = union, union
                        changed = True
    return found, families


def suspend(pid):
    if pid in (0, 4, os.getpid()):
        return False
    handle = kernel32.OpenProcess(PROCESS_SUSPEND_RESUME, False, pid)
    if not handle:
        return False
    try:
        return ntdll.NtSuspendProcess(handle) == 0
    finally:
        kernel32.CloseHandle(handle)


def resume(pid):
    handle = kernel32.OpenProcess(PROCESS_SUSPEND_RESUME, False, pid)
    if not handle:
        return False
    try:
        return ntdll.NtResumeProcess(handle) == 0
    finally:
        kernel32.CloseHandle(handle)


# -------------------------------------------------------------------------- audio
def audio_sessions(pid):
    """pycaw sessions belonging to pid; empty list when pycaw or the session is missing"""
    try:
        from pycaw.pycaw import AudioUtilities
    except Exception as exc:                      # noqa: BLE001 - report once, keep running
        log("pycaw 不可用：%s" % exc)
        return []
    out = []
    try:
        for session in AudioUtilities.GetAllSessions():
            proc = session.Process
            if proc is not None and proc.pid == pid:
                out.append(session)
    except Exception as exc:                      # noqa: BLE001
        log("枚举音频会话失败：%s" % exc)
    return out


def set_mute(pid, mute):
    ok = True
    sessions = audio_sessions(pid)
    if not sessions:
        return False
    for session in sessions:
        try:
            session.SimpleAudioVolume.SetMute(bool(mute), None)
        except Exception as exc:                  # noqa: BLE001
            log("设置静音失败 pid=%d：%s" % (pid, exc))
            ok = False
    return ok


def any_muted(pid):
    for session in audio_sessions(pid):
        try:
            if session.SimpleAudioVolume.GetMute():
                return True
        except Exception:                         # noqa: BLE001
            continue
    return False


# ------------------------------------------------------------------------- tracker
class Tracker(object):
    """owns the desired state per target process and applies it on changes only"""

    def __init__(self, config):
        self.cfg = config
        self.enabled = True
        self.bg_since = {}          # pid -> when it went to the background
        self.last_assert = {}       # pid -> last time we forced the mute state
        self.last_fg = None
        self.applied = {}          # pid -> dict(muted=bool, suspended=bool)
        self.last_status = ""
        self.per_target = {}
        # the poller runs on its own thread; without this, a tick that already read
        # enabled=True can mute right after the tray switched the feature off
        self._lock = threading.RLock()

    def _read_state_file(self):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return {}

    def _write_state_file(self):
        suspended = sorted(pid for pid, st in self.applied.items() if st.get("suspended"))
        try:
            with open(STATE_PATH, "w", encoding="utf-8") as handle:
                json.dump({"suspended": suspended}, handle)
        except OSError:
            pass

    def clear_stale_mute(self):
        """a hard kill may have left the target muted; the rule never wants to start muted"""
        for name, pids in find_pids(self.cfg.processes).items():
            for pid in pids:
                if set_mute(pid, False):
                    log("启动时清除残留静音 pid=%d" % pid)
                    self.applied.setdefault(pid, {"muted": False, "suspended": False})["muted"] = False

    def recover_from_crash(self):
        """resume whatever a previous run left suspended"""
        saved = self._read_state_file()
        for pid in saved.get("suspended", []):
            if process_alive(pid):
                if resume(pid):
                    log("恢复上次残留的暂停：pid=%d" % pid)
        self._write_state_file()

    def set_enabled(self, value):
        """turn the automatic behaviour on/off; switching it off hands the sound back
        immediately, otherwise the state stays muted until the target is refocused"""
        with self._lock:
            value = bool(value)
            if value == self.enabled:
                return
            self.enabled = value
            log("自动静音 %s" % ("开启" if value else "关闭"))
            if not value:
                self.release_all()

    def apply(self, pid, flags, muted, suspended):
        st = self.applied.setdefault(pid, {"muted": False, "suspended": False})
        # a switch being turned off for this target must still release it, so the
        # negative direction is never gated by the flags
        want_suspend = bool(suspended and flags["pause"])
        want_mute = bool(muted and flags["mute"])
        if want_suspend and not st["suspended"]:
            if suspend(pid):
                st["suspended"] = True
                log("暂停 pid=%d" % pid)
        elif not want_suspend and st["suspended"]:
            if resume(pid):
                st["suspended"] = False
                log("恢复 pid=%d" % pid)
        if want_mute and not st["muted"]:
            now = time.time()
            if st.get("mute_failed") and now < st.get("mute_retry_at", 0):
                pass                      # a failed attempt is retried once a second, not per tick
            elif set_mute(pid, True):
                st["muted"] = True
                st["mute_failed"] = False
                log("静音 pid=%d -> 是" % pid)
            else:
                # no audio session (silent screen, still loading) or the call failed
                st["mute_failed"] = True
                st["mute_retry_at"] = now + 1.0
        elif not want_mute and st["muted"]:
            if set_mute(pid, False):
                st["muted"] = False
                st["mute_failed"] = False
                log("静音 pid=%d -> 否" % pid)
        elif not want_mute:
            st["mute_failed"] = False
        # Windows stores a per-app mute; a freshly created session can be born muted
        # while our map believes it is audible. Re-push the wanted state periodically
        # so the tool, not the leftover registry bit, decides.
        now2 = time.time()
        if (now2 - self.last_assert.get(pid, 0.0)) >= REASSERT_S:
            self.last_assert[pid] = now2
            if set_mute(pid, want_mute):
                if st["muted"] != want_mute:
                    log("静音 pid=%d -> %s（重新确认）" % (pid, "是" if want_mute else "否"))
                st["muted"] = want_mute
                if not want_mute:
                    st["mute_failed"] = False

    def release_all(self):
        with self._lock:
            self._release_all_locked()

    def _release_all_locked(self):
        for pid, st in list(self.applied.items()):
            if st.get("suspended"):
                if resume(pid):
                    st["suspended"] = False
                    log("退出前恢复 pid=%d" % pid)
            if st.get("muted"):
                if set_mute(pid, False):
                    st["muted"] = False
                    log("退出前解除静音 pid=%d" % pid)
        self._write_state_file()

    def step(self):
        with self._lock:
            return self._step_locked()

    def _step_locked(self):
        fg, title = foreground_info()
        if fg != self.last_fg:
            self.last_fg = fg
            log("前台 pid=%s %s" % (fg, title[:40]))
        running, families = family_map(self.cfg.processes) if self.cfg.processes else ({}, {})
        if fg == 0 and running:
            # Windows gave us nothing: a fullscreen game often looks like this. Hold
            # the previous state instead of guessing, muting here is what killed his audio.
            self.last_status = "前台未知 · " + (self.last_status or "")
            return self.last_status
        # while *this* target's family (the exe or anything it launched) holds the
        # focus, it must not be muted: with a launcher + real game pair the window and
        # the audio can sit in different pids, and muting "the other one" killed sound
        status_bits = []
        for name, pids in running.items():
            flags = self.cfg.flags(name)
            for pid in pids:
                family = families.get(pid, {pid})
                background = fg not in family
                now = time.time()
                if background:
                    started = self.bg_since.setdefault(pid, now)
                    settled = (now - started) >= MUTE_DELAY_S
                else:
                    self.bg_since.pop(pid, None)
                    settled = False
                self.apply(pid, flags, self.enabled and background and settled,
                           self.enabled and background and settled)
        for pid in list(self.applied.keys()):
            if not process_alive(pid):
                self.applied.pop(pid, None)
        # report every configured target, not just the running ones, so it is obvious
        # which of several candidate exe names is the live one
        per = {}
        for name in self.cfg.processes:
            if name in running:
                labels = []
                for pid in running[name]:
                    labels.append(self._pid_label(pid, fg, families.get(pid, {pid})))
                per[name] = "/".join(sorted(set(labels)))
            else:
                per[name] = "未运行"
        self.per_target = per
        parts = ["%s：%s" % (name.split(".")[0], per[name]) for name in self.cfg.processes]
        status = " · ".join(parts) if parts else "未配置目标"
        if not self.enabled:
            status = "已停用 · " + status
        if status != getattr(self, "_logged_status", None):
            self._logged_status = status
            log("状态：" + status)
        self.last_status = status
        self._write_state_file()
        return status

    def _pid_label(self, pid, fg, family=None):
        state = self.applied.get(pid, {})
        if pid == fg:
            return "前台"
        if family and fg in family and not state.get("muted"):
            return "随前台"
        bits = []
        if state.get("suspended"):
            bits.append("暂停")
        if state.get("muted"):
            bits.append("静音")
        if not bits:
            # backgrounded but we could not silence it: usually no audio session at all
            bits.append("无音频会话" if state.get("mute_failed") else "后台有声")
        return "+".join(bits)


# ----------------------------------------------------------------------------- tray
def make_icon_image(enabled):
    """On/off must survive red-green colour blindness: the two states differ in
    luminance and shape (white speaker + sound arcs vs grey speaker + big cross),
    and the accent is blue versus neutral grey rather than green."""
    from PIL import Image, ImageDraw
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    bg = (24, 26, 31, 255)
    accent = (47, 129, 247, 255) if enabled else (150, 156, 166, 255)   # blue / grey
    glyph = (255, 255, 255, 255) if enabled else (158, 164, 174, 255)   # bright / dim
    draw.rounded_rectangle([3, 3, 60, 60], radius=13, fill=bg, outline=accent, width=4)
    draw.polygon([(15, 25), (24, 25), (35, 14), (35, 50), (24, 39), (15, 39)], fill=glyph)
    if enabled:
        draw.arc([36, 17, 56, 47], start=-55, end=55, fill=glyph, width=4)
        draw.arc([40, 23, 52, 41], start=-45, end=45, fill=accent, width=3)
    else:
        draw.line([(39, 19), (55, 45)], fill=accent, width=7)
        draw.line([(55, 19), (39, 45)], fill=accent, width=7)
    return img


def open_in_notepad(path):
    """Open a .log/.ini with Notepad.

    Two traps this avoids: those extensions have no file association (os.startfile
    raises WinError 1155), and a windowed PyInstaller build cannot hand valid std
    handles to subprocess. ShellExecuteW does neither - it just asks the shell.
    """
    if not os.path.exists(path):
        with open(path, "a", encoding="utf-8"):
            pass
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.ShellExecuteW.argtypes = [wt.HWND, wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, ctypes.c_int]
    shell32.ShellExecuteW.restype = ctypes.c_ssize_t
    result = shell32.ShellExecuteW(None, "open", "notepad.exe", '"%s"' % path, None, 1)
    if result <= 32:
        log("打开失败（ShellExecute 返回 %s）：%s" % (result, path))


def run_tray(tracker, stop_event):
    import pystray

    def safe(label, fn):
        def wrapper(icon, item):
            try:
                fn(icon, item)
            except Exception as exc:                 # noqa: BLE001
                log("托盘动作失败[%s]：%s" % (label, exc))
        return wrapper

    def toggle(icon, item):
        tracker.set_enabled(not tracker.enabled)
        icon.icon = make_icon_image(tracker.enabled)
        icon.update_menu()

    def open_log(icon, item):
        open_in_notepad(LOG_PATH)

    def open_conf(icon, item):
        open_in_notepad(CONF_PATH)

    def quit_(icon, item):
        stop_event.set()
        icon.stop()

    def toggle_text(item):
        # this single row is the whole on/off story: switching it off also hands the
        # sound back immediately, so a separate "restore" row would do the same thing
        if tracker.enabled:
            return "自动静音：已开启（点击关闭并恢复声音）"
        return "自动静音：已关闭（点击开启）"

    icon = pystray.Icon("FocusMute", make_icon_image(tracker.enabled), "FocusMute",
                        menu=pystray.Menu(
                            pystray.MenuItem("状态", None, enabled=False),
                            *[pystray.MenuItem(lambda item, n=name: "   " + n.split(".")[0] + "：" + tracker.per_target.get(n, "…"),
                                              None, enabled=False)
                              for name in tracker.cfg.processes],
                            pystray.Menu.SEPARATOR,
                            pystray.MenuItem(toggle_text, safe("toggle", toggle), default=True),
                            pystray.Menu.SEPARATOR,
                            pystray.MenuItem("打开日志", safe("log", open_log)),
                            pystray.MenuItem("编辑配置", safe("conf", open_conf)),
                            pystray.MenuItem("退出（会自动恢复声音和暂停）", safe("quit", quit_)),
                        ))

    def updater():
        ticks = 0
        while not stop_event.is_set():
            try:
                icon.title = "FocusMute · " + (tracker.last_status or "启动中")
                ticks += 1
                if ticks % 2 == 0:
                    # the status row inside the menu is only re-read when it rebuilds
                    icon.update_menu()
            except Exception:                       # noqa: BLE001
                pass
            time.sleep(1.0)

    threading.Thread(target=updater, daemon=True).start()
    icon.run()


# -------------------------------------------------------------------------- selftest
def selftest():
    problems = 0
    cfg = Config()
    print("config: processes=%s poll_ms=%d global mute=%s pause=%s"
          % (cfg.processes, cfg.poll_ms, cfg.do_mute, cfg.do_pause))
    for name in cfg.processes:
        flags = cfg.flags(name)
        print("  target %s -> mute=%s pause=%s" % (name, flags["mute"], flags["pause"]))
    if not cfg.processes:
        print("FAIL: focusmute.ini 里没有 process=")
        problems += 1
    for symbol in ("GetForegroundWindow", "GetWindowThreadProcessId", "OpenProcess", "CloseHandle"):
        if not hasattr(user32, symbol) and not hasattr(kernel32, symbol):
            print("FAIL: 缺少 Win32 接口", symbol)
            problems += 1
    for symbol in ("NtSuspendProcess", "NtResumeProcess"):
        if not hasattr(ntdll, symbol):
            print("FAIL: ntdll 缺少", symbol)
            problems += 1
    print("前台 pid =", foreground_pid())
    found = find_pids(cfg.processes)
    print("找到目标进程：", found or "无（先启动目标程序再自检音频会话）")
    try:
        import pycaw                                        # noqa: F401
        print("pycaw: ok")
    except Exception as exc:                                # noqa: BLE001
        print("FAIL: pycaw 导入失败：%s" % exc)
        problems += 1
    try:
        from pycaw.pycaw import AudioUtilities
        sessions = AudioUtilities.GetAllSessions()
        print("音频会话总数 =", len(sessions))
        for name, pids in found.items():
            for pid in pids:
                mine = [s for s in sessions if s.Process is not None and s.Process.pid == pid]
                print("  %s pid=%d 会话数=%d 当前静音=%s"
                      % (name, pid, len(mine), any(s.SimpleAudioVolume.GetMute() for s in mine) if mine else "-"))
                if not mine:
                    print("  注意：这个进程当前没有音频会话（游戏没发声时正常）")
    except Exception as exc:                                # noqa: BLE001
        print("FAIL: 枚举音频会话失败：%s" % exc)
        problems += 1
    try:
        import PIL                                          # noqa: F401
        import pystray                                      # noqa: F401
        print("pystray/pillow: ok")
    except Exception as exc:                                # noqa: BLE001
        print("FAIL: 托盘依赖缺失：%s" % exc)
        problems += 1
    print("RESULT problems=%d" % problems)
    return 1 if problems else 0


# ------------------------------------------------------------------------------ main
def acquire_single_instance():
    """double-clicking while the startup copy runs would give two pollers fighting"""
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
    kernel32.CreateMutexW.restype = wt.HANDLE
    handle = kernel32.CreateMutexW(None, False, "Global\FocusMute.singleton")
    if not handle:
        return None
    if ctypes.get_last_error() == 183:            # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return None
    return handle


def main():
    if "--selftest" in sys.argv:
        return selftest()

    instance = acquire_single_instance()
    if instance is None:
        log("已经有一个 FocusMute 在跑，这个实例退出")
        return 0

    cfg = Config()
    log("FocusMute 启动：processes=%s poll_ms=%d mute=%s pause=%s"
        % (cfg.processes, cfg.poll_ms, cfg.do_mute, cfg.do_pause))
    for name in cfg.processes:
        flags = cfg.flags(name)
        log("  目标 %s：mute=%s pause=%s" % (name, flags["mute"], flags["pause"]))
    if not cfg.processes:
        log("focusmute.ini 里没有 process=，什么都没得做。")
        return 2

    tracker = Tracker(cfg)
    tracker.recover_from_crash()
    tracker.clear_stale_mute()

    stop_event = threading.Event()

    def poll_loop():
        while not stop_event.is_set():
            try:
                tracker.step()
            except Exception as exc:                   # noqa: BLE001
                log("轮询出错：%s" % exc)
            stop_event.wait(cfg.poll_ms / 1000.0)

    threading.Thread(target=poll_loop, daemon=True).start()

    if "--no-tray" in sys.argv:
        try:
            while not stop_event.is_set():
                stop_event.wait(1.0)
                if tracker.last_status:
                    pass
        except KeyboardInterrupt:
            pass
    else:
        try:
            run_tray(tracker, stop_event)
        except Exception as exc:                       # noqa: BLE001
            log("托盘起不来（%s），退成无托盘模式" % exc)
            try:
                while not stop_event.is_set():
                    stop_event.wait(1.0)
            except KeyboardInterrupt:
                pass
    tracker.release_all()
    log("FocusMute 退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
