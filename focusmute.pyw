"""FocusMute - 目标程序切到后台就静音并暂停（各自可关），切回来恢复。

托盘图标看状态，右键有菜单。配置在同目录 focusmute.ini，可填多个程序名。
改完 ini 保存后会自动重载（约半秒），托盘菜单里也有「重载配置」，不用重启。
挂起的窗口点任务栏激活不了（Windows 不会激活无响应窗口），所以托盘菜单里每个游戏
那一行本身就是恢复入口：点哪一行就把哪个游戏恢复、解除静音并切回前台。
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
def ini_text(path):
    """Read the ini with one tolerance: an indented [target:xxx] header is still a
    header. configparser treats an indented line as a continuation of the previous
    value, so a single leading space swallowed the whole "pause = true" into
    poll_ms and the override silently never applied."""
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            raw = handle.read()
    except OSError:
        return None                      # unreadable: the caller must keep the old config
    lines = []
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            line = stripped
        lines.append(line)
    return "\n".join(lines)


def conf_signature():
    """(mtime, size) so the poller can notice the ini was saved"""
    try:
        st = os.stat(CONF_PATH)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _int_option(section, key, default, minimum):
    """int config value with a floor; a bad value falls back instead of crashing"""
    try:
        return max(minimum, int(str(section.get(key, default)).strip()))
    except (TypeError, ValueError):
        log("配置项 %s 不是整数，用默认值 %s" % (key, default))
        return default


class Config(object):
    def __init__(self, path=CONF_PATH):
        self.processes = []
        self.poll_ms = 250
        self.do_mute = True
        self.do_pause = False
        parser = configparser.ConfigParser(strict=False)
        text = ini_text(path)
        self.ok = text is not None
        parser.read_string(text or "", source=str(path))
        section = parser["general"] if parser.has_section("general") else {}
        raw = section.get("process", "")
        self.processes = [p.strip().lower() for p in raw.replace(";", ",").split(",") if p.strip()]
        try:
            self.poll_ms = max(50, int(section.get("poll_ms", "250")))
        except ValueError:
            self.poll_ms = 250
        self.do_mute = section.get("mute", "true").strip().lower() in ("1", "true", "yes", "on")
        self.do_pause = section.get("pause", "false").strip().lower() in ("1", "true", "yes", "on")
        # mute and pause have separate delays: cutting the sound after a beat avoids
        # a click-clack when briefly alt-tabbing, but a game that keeps running while
        # it is in the background can kill the player in that beat, so pausing defaults
        # to "as soon as the poller notices" (0 ms)
        self.delay_ms = _int_option(section, "delay_ms", 2000, 0)
        self.pause_delay_ms = _int_option(section, "pause_delay_ms", 0, 0)

        # every process starts with the global switches, then [target:<exe>]
        # sections override them one by one
        self.targets = {}
        for name in self.processes:
            self.targets[name] = {"mute": self.do_mute, "pause": self.do_pause,
                                  "delay_ms": self.delay_ms,
                                  "pause_delay_ms": self.pause_delay_ms}
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
            for key in ("delay_ms", "pause_delay_ms"):
                if body.get(key) is not None:
                    self.targets[name][key] = _int_option(body, key, self.targets[name][key], 0)

    def flags(self, name):
        return self.targets.get(name, {"mute": self.do_mute, "pause": self.do_pause,
                                       "delay_ms": self.delay_ms,
                                       "pause_delay_ms": self.pause_delay_ms})


# ---------------------------------------------------------------------- win32 bits
user32.GetWindowTextW.argtypes = [wt.HWND, ctypes.c_void_p, ctypes.c_int]
user32.GetWindowTextLengthW.argtypes = [wt.HWND]
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.IsIconic.argtypes = [wt.HWND]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.BringWindowToTop.argtypes = [wt.HWND]
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.SwitchToThisWindow.argtypes = [wt.HWND, wt.BOOL]
user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
kernel32.GetCurrentThreadId.restype = wt.DWORD


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


def find_main_window(pids):
    """biggest visible top-level window owned by any of these pids"""
    found = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def enum_proc(hwnd, lparam):
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and user32.IsWindowVisible(hwnd):
            rect = wt.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            width, height = rect.right - rect.left, rect.bottom - rect.top
            if width >= 320 and height >= 200:
                found.append((width * height, hwnd))
        return True

    user32.EnumWindows(enum_proc, 0)
    return max(found)[1] if found else None


def activate_window(pids):
    """bring that window back to the front. A suspended game never receives the
    click, so the tray action has to hand it the foreground itself."""
    hwnd = find_main_window(pids)
    if not hwnd:
        return False
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)                  # SW_RESTORE
    fg = user32.GetForegroundWindow()
    fg_thread = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    this_thread = kernel32.GetCurrentThreadId()
    attached = bool(fg_thread and fg_thread != this_thread and
                    user32.AttachThreadInput(fg_thread, this_thread, True))
    try:
        user32.BringWindowToTop(hwnd)
        ok = bool(user32.SetForegroundWindow(hwnd))
        if not ok or user32.GetForegroundWindow() != hwnd:
            user32.SwitchToThisWindow(hwnd, True)
            ok = user32.GetForegroundWindow() == hwnd
    finally:
        if attached:
            user32.AttachThreadInput(fg_thread, this_thread, False)
    return ok


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


_fail_log_at = {}


def log_failure(what, pid, detail):
    """a suspend/resume that keeps failing would otherwise write a line every tick"""
    key = (what, pid)
    now = time.time()
    if now - _fail_log_at.get(key, 0.0) >= 60:
        _fail_log_at[key] = now
        log("%s失败 pid=%d：%s" % (what, pid, detail))


def suspend(pid):
    if pid in (0, 4, os.getpid()):
        return False
    handle = kernel32.OpenProcess(PROCESS_SUSPEND_RESUME, False, pid)
    if not handle:
        log_failure("暂停", pid, "OpenProcess 被拒 err=%d（权限不够？）" % ctypes.get_last_error())
        return False
    try:
        rc = ntdll.NtSuspendProcess(handle)
        if rc != 0:
            log_failure("暂停", pid, "NtSuspendProcess=0x%08X" % (rc & 0xFFFFFFFF))
        return rc == 0
    finally:
        kernel32.CloseHandle(handle)


def resume(pid):
    handle = kernel32.OpenProcess(PROCESS_SUSPEND_RESUME, False, pid)
    if not handle:
        log_failure("恢复", pid, "OpenProcess 被拒 err=%d" % ctypes.get_last_error())
        return False
    try:
        rc = ntdll.NtResumeProcess(handle)
        if rc != 0:
            log_failure("恢复", pid, "NtResumeProcess=0x%08X" % (rc & 0xFFFFFFFF))
        return rc == 0
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
        self.on_reload = None       # tray sets this so a reload rebuilds its menu
        # pids the user resumed by hand: a suspended window cannot be clicked back,
        # so auto pause stays disarmed for them until they are in front again
        self.manual_hold = set()
        # pids that have held the foreground at least once. A game that is still
        # starting (launcher has not shown its window yet) must not be suspended
        # before that: freezing it during initialisation can hang the whole game.
        self.armed = set()
        self._unarmed_logged = set()
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
        """a hard kill may have left the target muted. Clear it only while the target is
        in front (or Windows cannot tell): unmuting a background target here would let
        its sound play for delay_ms until the poller mutes it again."""
        fg = foreground_pid()
        families = family_map(self.cfg.processes)[1] if self.cfg.processes else {}
        for name, pids in find_pids(self.cfg.processes).items():
            for pid in pids:
                if fg and fg not in families.get(pid, {pid}):
                    continue
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
            log("自动静音+暂停 %s" % ("开启" if value else "关闭"))
            if not value:
                self.release_all()

    def restore_target(self, name):
        """per-game escape hatch: resume this one, give its sound back and bring it to
        the front. Clicking a suspended window does not activate it, so the tray menu
        has to do it. Auto pause stays disarmed for it until it has been in front."""
        pids = find_pids([name]).get(name, [])
        if not pids:
            log("恢复 %s：没有在运行" % name)
            return False
        with self._lock:
            for pid in pids:
                st = self.applied.get(pid)
                if st and st.get("suspended"):
                    if resume(pid):
                        st["suspended"] = False
                        st["pause_failed"] = False
                        log("手动恢复暂停：%s pid=%d" % (name, pid))
                    else:
                        log("手动恢复失败：%s pid=%d" % (name, pid))
                if st and st.get("muted"):
                    if set_mute(pid, False):
                        st["muted"] = False
                        log("手动解除静音：%s pid=%d" % (name, pid))
                self.manual_hold.add(pid)
            self._write_state_file()
        if activate_window(set(pids)):
            log("已恢复并切回 %s" % name)
        else:
            log("%s 已恢复，但没能自动切到前台（点一下游戏即可）" % name)
        return True

    def reload_config(self):
        """re-read focusmute.ini; the tray item and the auto reload both come here"""
        try:
            new_cfg = Config()
        except Exception as exc:                      # noqa: BLE001
            log("重载配置失败，继续用旧配置：%s" % exc)
            return False
        return self.reconfigure(new_cfg)

    def reconfigure(self, new_cfg):
        """swap in a freshly parsed config without restarting: a target that was
        removed or switched off is resumed/unmuted immediately"""
        if not getattr(new_cfg, "ok", True):
            log("重载配置：ini 打不开，继续用旧配置")
            return False
        with self._lock:
            exe_of = exe_name_by_pid()
            for pid in list(self.applied):
                st = self.applied[pid]
                name = exe_of.get(pid, "")
                if name not in new_cfg.processes:
                    self._release_pid(pid, "已从配置移除")
                    continue
                flags = new_cfg.flags(name)
                if st.get("suspended") and not flags["pause"]:
                    if resume(pid):
                        st["suspended"] = False
                        log("重载配置：恢复 pid=%d（pause 已关）" % pid)
                if st.get("muted") and not flags["mute"]:
                    if set_mute(pid, False):
                        st["muted"] = False
                        log("重载配置：解除静音 pid=%d（mute 已关）" % pid)
                st.pop("pause_retry_at", None)
            self.cfg = new_cfg
            self.last_status = ""
            self._logged_status = None
            self.per_target = {}
            self._write_state_file()
            if not new_cfg.processes:
                log("重载配置：process= 是空的，什么都不做")
            else:
                log("重载配置：processes=%s poll_ms=%d" % (new_cfg.processes, new_cfg.poll_ms))
                for name in new_cfg.processes:
                    flags = new_cfg.flags(name)
                    log("  目标 %s：mute=%s(%dms) pause=%s(%dms)"
                        % (name, flags["mute"], flags["delay_ms"],
                           flags["pause"], flags["pause_delay_ms"]))
        callback = self.on_reload
        if callback is not None:
            try:
                callback()
            except Exception as exc:                  # noqa: BLE001
                log("重载后刷新托盘失败：%s" % exc)
        return True

    def _release_pid(self, pid, reason):
        st = self.applied.get(pid)
        if st is None:
            return
        released = True
        if st.get("suspended"):
            if resume(pid):
                st["suspended"] = False
                log("解除暂停 pid=%d（%s）" % (pid, reason))
            else:
                released = False                  # keep tracking it so quit/next start can retry
        if st.get("muted"):
            if set_mute(pid, False):
                st["muted"] = False
                log("解除静音 pid=%d（%s）" % (pid, reason))
            else:
                released = False
        if released:
            self.applied.pop(pid, None)
            self.bg_since.pop(pid, None)
            self.last_assert.pop(pid, None)
            self.manual_hold.discard(pid)
            self.armed.discard(pid)
            self._unarmed_logged.discard(pid)

    def apply(self, pid, flags, muted, suspended):
        st = self.applied.setdefault(pid, {"muted": False, "suspended": False})
        # a switch being turned off for this target must still release it, so the
        # negative direction is never gated by the flags
        want_suspend = bool(suspended and flags["pause"])
        want_mute = bool(muted and flags["mute"])
        if want_suspend and not st["suspended"]:
            now = time.time()
            if now >= st.get("pause_retry_at", 0.0):
                if suspend(pid):
                    st["suspended"] = True
                    st["pause_failed"] = False
                    log("暂停 pid=%d" % pid)
                else:
                    # retry every couple of seconds instead of every tick; the reason
                    # itself is written by suspend() once a minute
                    st["pause_failed"] = True
                    st["pause_retry_at"] = now + 2.0
        elif not want_suspend and st["suspended"]:
            if resume(pid):
                st["suspended"] = False
                st["pause_failed"] = False
                log("恢复 pid=%d" % pid)
        elif not want_suspend:
            st["pause_failed"] = False
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
        self.manual_hold.clear()              # re-enabling starts from a clean slate

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
                    elapsed = now - started
                    settled_mute = elapsed >= flags["delay_ms"] / 1000.0
                    settled_pause = elapsed >= flags["pause_delay_ms"] / 1000.0
                    if pid not in self.armed and pid not in self._unarmed_logged:
                        self._unarmed_logged.add(pid)
                        log("%s pid=%d 还没在前台露过面，先不静音也不挂起（等它显示出来）" % (name, pid))
                else:
                    self.bg_since.pop(pid, None)
                    settled_mute = settled_pause = False
                    self.manual_hold.discard(pid)     # back in front: arm auto pause again
                    self.armed.add(pid)
                    self._unarmed_logged.discard(pid)
                held = background and pid in self.manual_hold
                if pid in self.armed:
                    # an unarmed pid is not touched at all: no mute, no unmute, no
                    # suspend. It is armed the first moment it holds the foreground.
                    self.apply(pid, flags,
                               self.enabled and background and settled_mute,
                               self.enabled and background and settled_pause and not held)
        for pid in set(self.applied) | self.armed:
            if not process_alive(pid):
                self.applied.pop(pid, None)
                self.bg_since.pop(pid, None)
                self.last_assert.pop(pid, None)
                self.manual_hold.discard(pid)     # a reused pid must not inherit a hold
                self.armed.discard(pid)
                self._unarmed_logged.discard(pid)
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
        if pid not in self.armed:
            return "待前台"          # never shown up yet: left alone until it does
        bits = []
        if state.get("suspended"):
            bits.append("暂停")
        elif state.get("pause_failed"):
            bits.append("暂停失败")
        if pid in self.manual_hold and not state.get("suspended"):
            bits.append("已放行")
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

    def notify(icon, message):
        try:
            icon.notify(message, "FocusMute")
        except Exception:                            # noqa: BLE001 - balloon is optional
            pass

    def reload_conf(icon, item):
        # the rebuild itself happens in tracker.on_reload, so the auto reload gets it too
        if tracker.reload_config():
            notify(icon, "配置已重载")
        else:
            notify(icon, "重载失败，详见日志")

    def toggle_text(item):        # this single row is the whole on/off story: switching it off also hands the
        # sound and the suspended process back immediately, so a separate "restore"
        # row would do the same thing
        if tracker.enabled:
            return "自动静音+暂停：已开启（点击关闭并恢复）"
        return "自动静音+暂停：已关闭（点击开启）"

    def build_menu():
        # the per-game status rows double as the way back: a suspended window cannot
        # be activated by clicking it, so clicking the row resumes and brings it front
        items = [pystray.MenuItem("状态（点游戏名：恢复并切回）", None, enabled=False)]
        items += [pystray.MenuItem(
                      lambda item, n=name: "   " + n.split(".")[0] + "：" + tracker.per_target.get(n, "…"),
                      safe("restore", lambda icon, item, n=name: tracker.restore_target(n)),
                      enabled=lambda item, n=name: tracker.per_target.get(n, "未运行") != "未运行")
                  for name in tracker.cfg.processes]
        items += [
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(toggle_text, safe("toggle", toggle), default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("重载配置（改完 ini 立即生效）", safe("reload", reload_conf)),
            pystray.MenuItem("打开日志", safe("log", open_log)),
            pystray.MenuItem("编辑配置", safe("conf", open_conf)),
            pystray.MenuItem("退出（会自动恢复声音和暂停）", safe("quit", quit_)),
        ]
        return pystray.Menu(*items)

    icon = pystray.Icon("FocusMute", make_icon_image(tracker.enabled), "FocusMute",
                        menu=build_menu())

    def refresh_menu():
        # targets may have been added or removed by the reload
        icon.menu = build_menu()
        icon.update_menu()

    tracker.on_reload = refresh_menu

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
    print("config: processes=%s poll_ms=%d global mute=%s pause=%s delay_ms=%d pause_delay_ms=%d"
          % (cfg.processes, cfg.poll_ms, cfg.do_mute, cfg.do_pause, cfg.delay_ms, cfg.pause_delay_ms))
    for name in cfg.processes:
        flags = cfg.flags(name)
        print("  target %s -> mute=%s pause=%s delay_ms=%d pause_delay_ms=%d"
              % (name, flags["mute"], flags["pause"], flags["delay_ms"], flags["pause_delay_ms"]))
    if not cfg.processes:
        print("FAIL: focusmute.ini 里没有 process=" if cfg.ok else "FAIL: focusmute.ini 读不到")
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
    log("FocusMute 启动：processes=%s poll_ms=%d mute=%s pause=%s delay_ms=%d pause_delay_ms=%d"
        % (cfg.processes, cfg.poll_ms, cfg.do_mute, cfg.do_pause, cfg.delay_ms, cfg.pause_delay_ms))
    for name in cfg.processes:
        flags = cfg.flags(name)
        log("  目标 %s：mute=%s(%dms) pause=%s(%dms)"
            % (name, flags["mute"], flags["delay_ms"], flags["pause"], flags["pause_delay_ms"]))
    if not cfg.processes:
        log("focusmute.ini 里没有 process=，什么都没得做。" if cfg.ok else "focusmute.ini 读不到。")
        return 2

    tracker = Tracker(cfg)
    tracker.recover_from_crash()
    tracker.clear_stale_mute()

    stop_event = threading.Event()
    conf_sig = conf_signature()
    pending_sig = None

    def poll_loop():
        nonlocal conf_sig, pending_sig
        while not stop_event.is_set():
            try:
                sig = conf_signature()
                if sig is not None and sig != conf_sig:
                    # an editor may write the file in several steps: only reload once
                    # the same signature has been seen twice (two ticks apart)
                    if sig == pending_sig:
                        conf_sig, pending_sig = sig, None
                        tracker.reload_config()
                    else:
                        pending_sig = sig
                tracker.step()
            except Exception as exc:                   # noqa: BLE001
                log("轮询出错：%s" % exc)
            stop_event.wait(tracker.cfg.poll_ms / 1000.0)

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
