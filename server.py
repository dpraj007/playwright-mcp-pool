#!/usr/bin/env python3
"""browserpool - an MCP server that fronts a pool of official @playwright/mcp
backends, each launched --isolated and seeded with the same logged-in
--storage-state.

One @playwright/mcp server = one browser, one tab, serial calls, no session
keys, so two parallel tool calls collide on the same tab. This server keeps up
to MAX backends alive and hands the agent a session handle per task:

    browser_new_session          -> "s1"   (pool auto-picks a free browser)
    browser_navigate(session=s1) -> routed to that browser
    browser_close_session(s1)    -> frees the slot

Every upstream @playwright/mcp tool is re-exported unchanged except for an
added, required `session` argument, so the agent keeps the real snapshot/ref
engine (Playwright Python has no public click-by-ref API, so reimplementing
the engine from scratch was a dead end).

Raw newline-delimited JSON-RPC over stdio, no third-party deps. Threaded: each
backend has its own lock, so calls to different sessions run concurrently.

Config via env:
  BROWSERPOOL_MAX           max concurrent browsers              (default 5)
  BROWSERPOOL_HEADLESS      "0" = visible windows, else headless (default 1)
  BROWSERPOOL_STATE         storage-state login seed   (default ./state.json)
  BROWSERPOOL_CONFIG        @playwright/mcp --config json
                            (default ./config/playwright-mcp.json if present)
  BROWSERPOOL_IDLE_TIMEOUT  seconds before an idle session is reaped,
                            0 disables                           (default 3600)
  BROWSERPOOL_PACKAGE       backend spec   (default @playwright/mcp@latest)
  BROWSERPOOL_TILE          "0" disables window tiling in headed mode (default 1)
  BROWSERPOOL_TILE_COLS     tiled windows per row     (default: fit to screen)
  BROWSERPOOL_WINDOW_SIZE   tiled window size, WxH    (default: fit to screen)
  BROWSERPOOL_SCREEN        screen size to fit into, WxH  (default: measured)
"""
import atexit
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

VERSION = "1.3.0"
PROTO = "2024-11-05"
HERE = os.path.dirname(os.path.abspath(__file__))

STATE = os.environ.get("BROWSERPOOL_STATE", os.path.join(HERE, "state.json"))
CONFIG = os.environ.get("BROWSERPOOL_CONFIG",
                        os.path.join(HERE, "config", "playwright-mcp.json"))
MAX = int(os.environ.get("BROWSERPOOL_MAX", "5"))
HEADLESS = os.environ.get("BROWSERPOOL_HEADLESS", "1") != "0"
IDLE_TIMEOUT = int(os.environ.get("BROWSERPOOL_IDLE_TIMEOUT", "3600"))
PACKAGE = os.environ.get("BROWSERPOOL_PACKAGE", "@playwright/mcp@latest")
TILE = os.environ.get("BROWSERPOOL_TILE", "1") != "0"
# npx is a .cmd shim on Windows and only resolves through the shell there.
USE_SHELL = os.name == "nt"

MIN_W, MIN_H = 480, 360
FALLBACK_SCREEN = (1920, 1080)


def _parse_wh(raw):
    try:
        w, h = str(raw).lower().split("x")
        return int(w), int(h)
    except Exception:
        return None


COLS_OVERRIDE = os.environ.get("BROWSERPOOL_TILE_COLS")
SIZE_OVERRIDE = _parse_wh(os.environ.get("BROWSERPOOL_WINDOW_SIZE", ""))
SCREEN_OVERRIDE = _parse_wh(os.environ.get("BROWSERPOOL_SCREEN", ""))

_TMPDIR = None
_TILE_CONFIGS = {}
_TILE_LOCK = threading.Lock()
_MEASURED_SCREEN = None     # filled in from a real browser at schema boot
_LAYOUT = None              # (cols, win_w, win_h), computed once


def log(*a):
    print("[browserpool]", *a, file=sys.stderr, flush=True)


def _cleanup_tmp():
    if _TMPDIR:
        shutil.rmtree(_TMPDIR, ignore_errors=True)


atexit.register(_cleanup_tmp)


def os_screen():
    """Last-resort screen size. Deliberately distrusted: on a scaled Windows
    desktop this process is DPI-virtualized and reports the logical size (e.g.
    1280x800) while Chromium, launched with --force-device-scale-factor=1,
    positions windows in physical pixels (2560x1600). Only used when a real
    browser measurement is unavailable."""
    try:
        if os.name == "nt":
            import ctypes
            try:
                ctypes.windll.user32.SetProcessDpiAwarenessContext(-4)
            except Exception:
                pass
            u = ctypes.windll.user32
            w, h = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
            if w > 0 and h > 0:
                return w, h
        else:
            import tkinter
            root = tkinter.Tk()
            w, h = root.winfo_screenwidth(), root.winfo_screenheight()
            root.destroy()
            if w > 0 and h > 0:
                return w, int(h * 0.95)   # leave room for a menu bar / panel
    except Exception:
        pass
    return None


def screen_size():
    if SCREEN_OVERRIDE:
        return SCREEN_OVERRIDE
    if _MEASURED_SCREEN:
        return _MEASURED_SCREEN
    return os_screen() or FALLBACK_SCREEN


def choose_grid(n, sw, sh):
    """Pick the column count that fills the screen best for n windows.

    Scored by usable area, penalised for extreme aspect ratios - otherwise
    "5 columns of 512x1504 slivers" wins on raw area and is useless to look at.
    """
    target_ar = 1.45
    best = None
    for cols in range(1, n + 1):
        rows = -(-n // cols)
        w, h = sw // cols, sh // rows
        if w < MIN_W or h < MIN_H:
            continue
        ar = float(w) / float(h)
        penalty = min(ar, target_ar) / max(ar, target_ar)
        score = w * h * penalty
        if best is None or score > best[0]:
            best = (score, cols, w, h)
    if best:
        return best[1], best[2], best[3]
    # Too many windows for the screen at a usable size: pack at the minimum
    # and let them overlap rather than shrinking into unreadable slivers.
    cols = max(1, min(n, sw // MIN_W))
    rows = max(1, -(-n // cols))
    return cols, max(MIN_W, sw // cols), max(MIN_H, sh // rows)


def layout():
    """(cols, window_w, window_h), computed once. Explicit env wins over fit."""
    global _LAYOUT
    with _TILE_LOCK:
        if _LAYOUT is not None:
            return _LAYOUT
        sw, sh = screen_size()
        cols, w, h = choose_grid(MAX, sw, sh)
        if COLS_OVERRIDE:
            try:
                cols = max(1, int(COLS_OVERRIDE))
                rows = -(-MAX // cols)
                w, h = sw // cols, sh // rows
            except ValueError:
                pass
        if SIZE_OVERRIDE:
            w, h = SIZE_OVERRIDE
        _LAYOUT = (cols, w, h)
        log("tiling %d windows as %dx%d grid of %dx%d in a %dx%d screen%s"
            % (MAX, cols, -(-MAX // cols), w, h, sw, sh,
               " (measured)" if _MEASURED_SCREEN else ""))
        return _LAYOUT


def tiled_config(slot):
    """A per-slot copy of the base config that positions the window.

    @playwright/mcp takes launch arguments only through --config, and the pool
    hands every backend the same file, so headed windows all land on top of
    each other. Each slot gets its own generated config instead, offset into a
    grid, so a headed pool tiles rather than stacks.
    """
    global _TMPDIR
    _layout_cache = layout()          # must not be called while holding the lock
    with _TILE_LOCK:
        if slot in _TILE_CONFIGS:
            return _TILE_CONFIGS[slot]
        base = {}
        if CONFIG and os.path.exists(CONFIG):
            try:
                with open(CONFIG, encoding="utf-8") as f:
                    base = json.load(f)
            except Exception as e:
                log("could not read base config %s: %s" % (CONFIG, e))
        browser = base.setdefault("browser", {})
        launch = browser.setdefault("launchOptions", {})
        args = [a for a in launch.get("args", [])
                if not a.startswith(("--window-position", "--window-size"))]
        cols, win_w, win_h = _layout_cache
        x = (slot % cols) * win_w
        y = (slot // cols) * win_h
        args += ["--window-position=%d,%d" % (x, y),
                 "--window-size=%d,%d" % (win_w, win_h)]
        launch["args"] = args
        if _TMPDIR is None:
            _TMPDIR = tempfile.mkdtemp(prefix="browserpool-")
        path = os.path.join(_TMPDIR, "config-slot%d.json" % slot)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(base, f)
        _TILE_CONFIGS[slot] = path
        return path


def backend_args(slot=None):
    args = ["npx", "-y", PACKAGE, "--isolated"]
    if HEADLESS:
        args.append("--headless")
    if STATE and os.path.exists(STATE):
        args += ["--storage-state", STATE]
    # Tiling only means anything when there are visible windows to tile.
    cfg = tiled_config(slot) if (TILE and not HEADLESS and slot is not None) else CONFIG
    if cfg and os.path.exists(cfg):
        args += ["--config", cfg]
    return args


class Backend:
    """One @playwright/mcp child process plus its MCP client handshake."""

    def __init__(self, sid, slot=None):
        self.sid = sid
        self.slot = slot
        self.window_state = "foreground" if not HEADLESS else "headless"
        self.lock = threading.Lock()
        self.last_used = time.time()
        self._id = itertools.count(1)
        self.p = subprocess.Popen(
            backend_args(slot), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
            bufsize=1, shell=USE_SHELL)
        self._rpc("initialize", {"protocolVersion": PROTO, "capabilities": {},
                                 "clientInfo": {"name": "browserpool",
                                                "version": VERSION}})
        self._notify("notifications/initialized")

    def _send(self, obj):
        self.p.stdin.write(json.dumps(obj) + "\n")
        self.p.stdin.flush()

    def _notify(self, method, params=None):
        m = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            m["params"] = params
        self._send(m)

    def _rpc(self, method, params=None, timeout=180):
        mid = next(self._id)
        m = {"jsonrpc": "2.0", "id": mid, "method": method}
        if params is not None:
            m["params"] = params
        self._send(m)
        end = time.time() + timeout
        while time.time() < end:
            line = self.p.stdout.readline()
            if not line:
                raise RuntimeError("backend closed")
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("id") == mid:
                return obj
        raise TimeoutError("backend " + method + " timed out after %ds" % timeout)

    def call_tool(self, name, args, timeout=180):
        with self.lock:
            self.last_used = time.time()
            try:
                return self._rpc("tools/call",
                                 {"name": name, "arguments": args},
                                 timeout=timeout)
            finally:
                self.last_used = time.time()

    def list_tools(self):
        with self.lock:
            return self._rpc("tools/list", {}).get("result", {}).get("tools", [])

    def idle_for(self):
        return time.time() - self.last_used

    def close(self):
        try:
            self.call_tool("browser_close", {}, timeout=20)
        except Exception:
            pass
        try:
            self.p.terminate()
        except Exception:
            pass


class Pool:
    def __init__(self):
        self.sessions = {}          # sid -> Backend | "spawning"
        self.reserved = set()       # grid slots claimed by in-flight spawns
        self.lock = threading.Lock()
        self.counter = itertools.count(1)
        self.backend_tools = None   # cached upstream schema list

    def ensure_schema(self):
        """Boot one throwaway backend to read the upstream tool catalog.

        While it is up, and only if windows are visible, ask it how big the
        screen actually is. A browser measuring its own display is the only
        source that agrees with the coordinate space Chromium then positions
        windows in - see os_screen() for why the OS is not asked first.
        """
        global _MEASURED_SCREEN
        if self.backend_tools is None:
            tmpl = Backend("schema")
            try:
                self.backend_tools = tmpl.list_tools()
                if TILE and not HEADLESS and not SCREEN_OVERRIDE:
                    try:
                        tmpl.call_tool("browser_navigate", {"url": "about:blank"},
                                       timeout=60)
                        r = tmpl.call_tool(
                            "browser_evaluate",
                            {"function": "() => screen.availWidth + 'x' + screen.availHeight"},
                            timeout=60)
                        txt = json.dumps(r.get("result", {}))
                        m = re.search(r"(\d{3,5})\s*x\s*(\d{3,5})", txt)
                        if m:
                            _MEASURED_SCREEN = (int(m.group(1)), int(m.group(2)))
                            log("measured screen: %dx%d" % _MEASURED_SCREEN)
                    except Exception as e:
                        log("screen measurement failed (%s); falling back" % e)
            finally:
                tmpl.close()
        return self.backend_tools

    def _free_slot(self):
        """Lowest unused grid position, so closed windows' slots get reused."""
        taken = {be.slot for be in self.sessions.values()
                 if be != "spawning" and be.slot is not None}
        taken |= self.reserved
        for i in range(MAX):
            if i not in taken:
                return i
        return 0

    def new_session(self):
        with self.lock:
            if len(self.sessions) >= MAX:
                raise RuntimeError(
                    "pool at MAX=%d (%d active, 0 free). Close one with "
                    "browser_close_session first, or raise BROWSERPOOL_MAX."
                    % (MAX, len(self.sessions)))
            sid = "s%d" % next(self.counter)
            slot = self._free_slot()
            self.reserved.add(slot)
            self.sessions[sid] = "spawning"
        try:
            be = Backend(sid, slot)
        except Exception:
            with self.lock:
                self.sessions.pop(sid, None)   # never leak the reserved slot
                self.reserved.discard(slot)
            raise
        with self.lock:
            self.sessions[sid] = be
            self.reserved.discard(slot)
        return sid

    def get(self, sid):
        be = self.sessions.get(sid)
        if be is None or be == "spawning":
            raise KeyError(sid)
        return be

    def close_session(self, sid):
        with self.lock:
            be = self.sessions.pop(sid, None)
        if be and be != "spawning":
            be.close()
            return True
        return False

    def reap_idle(self):
        if IDLE_TIMEOUT <= 0:
            return []
        with self.lock:
            stale = [sid for sid, be in self.sessions.items()
                     if be != "spawning" and be.idle_for() > IDLE_TIMEOUT]
        for sid in stale:
            log("reaping idle session %s (>%ds)" % (sid, IDLE_TIMEOUT))
            self.close_session(sid)
        return stale

    def close_all(self):
        with self.lock:
            sids = list(self.sessions.keys())
        for sid in sids:
            self.close_session(sid)

    def status(self):
        with self.lock:
            return {"max": MAX,
                    "active": list(self.sessions.keys()),
                    "slots": {sid: be.slot for sid, be in self.sessions.items()
                              if be != "spawning"},
                    "windows": {sid: be.window_state
                                for sid, be in self.sessions.items()
                                if be != "spawning"},
                    "free": MAX - len(self.sessions),
                    "headless": HEADLESS,
                    "tiling": TILE and not HEADLESS,
                    "grid": "%dx%d of %dx%d" % (layout()[0], -(-MAX // layout()[0]),
                                                layout()[1], layout()[2])
                            if (TILE and not HEADLESS) else None,
                    "login_seed": os.path.exists(STATE),
                    "idle_timeout": IDLE_TIMEOUT,
                    "version": VERSION}


POOL = Pool()
atexit.register(POOL.close_all)

# ---- tool catalog ----------------------------------------------------------
POOL_TOOLS = [
    {"name": "browser_new_session",
     "description": ("Allocate a browser from the pool and return its session id "
                     "(e.g. 's1'). Call this FIRST for each independent/parallel "
                     "browser task; the pool auto-picks a free browser (never "
                     "choose an instance yourself). Pass the returned id as "
                     "`session` to every later browser_* call. Up to %d run "
                     "concurrently." % MAX),
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "browser_close_session",
     "description": "Release a pool browser back (frees a slot). Call when the task is done.",
     "inputSchema": {"type": "object",
                     "properties": {"session": {"type": "string"}},
                     "required": ["session"]}},
    {"name": "browser_list_sessions",
     "description": "Show pool status: max, active session ids, free slots, login seed state.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "browser_bring_to_front",
     "description": ("Show this session's window to the human: restore it if "
                     "minimised, raise it above the others and focus it. The "
                     "SAME tab keeps running - nothing is reloaded and no state "
                     "is lost. You do NOT need this to read or drive the page; "
                     "use it only when a person should watch or take over. "
                     "Pair with browser_send_to_back to return it to the "
                     "background. No-op when the pool is headless."),
     "inputSchema": {"type": "object",
                     "properties": {"session": {"type": "string"}},
                     "required": ["session"]}},
    {"name": "browser_send_to_back",
     "description": ("Put this session's window back in the background "
                     "(minimised) without closing it. The tab stays live and "
                     "every browser_* tool keeps working on it exactly as "
                     "before - this only stops it covering the human's screen. "
                     "No-op when the pool is headless."),
     "inputSchema": {"type": "object",
                     "properties": {"session": {"type": "string"}},
                     "required": ["session"]}},
]

# Neither raising nor minimising has an upstream tool, so both reach the real
# window through the backend's raw `page`. Restore-then-raise is deliberate:
# bringToFront() alone does not un-minimise a window.
BRING_TO_FRONT_JS = """async (page) => {
  const s = await page.context().newCDPSession(page);
  const { windowId } = await s.send('Browser.getWindowForTarget');
  await s.send('Browser.setWindowBounds', { windowId, bounds: { windowState: 'normal' } });
  await page.bringToFront();
  return page.url();
}"""

SEND_TO_BACK_JS = """async (page) => {
  const s = await page.context().newCDPSession(page);
  const { windowId } = await s.send('Browser.getWindowForTarget');
  await s.send('Browser.setWindowBounds', { windowId, bounds: { windowState: 'minimized' } });
  return page.url();
}"""


def catalog():
    """Pool tools, plus every upstream tool with a required `session` arg."""
    tools = list(POOL_TOOLS)
    for t in POOL.ensure_schema():
        t2 = json.loads(json.dumps(t))  # deep copy
        schema = t2.setdefault("inputSchema", {"type": "object", "properties": {}})
        props = schema.setdefault("properties", {})
        props["session"] = {"type": "string",
                            "description": "pool session id from browser_new_session"}
        req = schema.setdefault("required", [])
        if "session" not in req:
            req.insert(0, "session")
        t2["description"] = "[pool] " + t2.get("description", t2["name"])
        tools.append(t2)
    return tools


def text_result(s, is_error=False):
    return {"content": [{"type": "text", "text": s}], "isError": is_error}


def handle_call(name, args):
    if name == "browser_new_session":
        sid = POOL.new_session()
        return text_result(
            "session=%s ready (isolated, headless=%s, login seed=%s). Pass "
            'session="%s" to browser_* calls; close with browser_close_session.'
            % (sid, HEADLESS, "yes" if os.path.exists(STATE) else "none", sid))
    if name == "browser_close_session":
        ok = POOL.close_session(args.get("session", ""))
        return text_result("closed" if ok else "no such session")
    if name == "browser_list_sessions":
        return text_result(json.dumps(POOL.status()))
    if name in ("browser_bring_to_front", "browser_send_to_back"):
        to_front = name == "browser_bring_to_front"
        sid = args.get("session") or ""
        if HEADLESS:
            return text_result(
                "no-op: the pool is headless, so there is no window to %s. The "
                "tab is still fully usable - browser_snapshot and "
                "browser_take_screenshot work regardless. For visible windows "
                "set BROWSERPOOL_HEADLESS=0 and restart the server."
                % ("raise" if to_front else "hide"))
        try:
            be = POOL.get(sid)
        except KeyError:
            return text_result("error: unknown session %s; call browser_new_session"
                               % sid, True)
        resp = be.call_tool(
            "browser_run_code_unsafe",
            {"code": BRING_TO_FRONT_JS if to_front else SEND_TO_BACK_JS},
            timeout=60)
        if "error" in resp:
            return text_result(
                "%s failed (needs the backend's run-code capability): %s"
                % (name, json.dumps(resp["error"])), True)
        be.window_state = "foreground" if to_front else "background"
        return text_result(
            "session %s is now in the %s (slot %s). The tab is unchanged and "
            "still driveable." % (be.sid, be.window_state,
                                  be.slot if be.slot is not None else "-"))

    # passthrough to a session's backend
    sid = args.pop("session", None)
    if not sid:
        return text_result(
            "error: missing `session` - call browser_new_session first", True)
    try:
        be = POOL.get(sid)
    except KeyError:
        return text_result(
            "error: unknown session %s; call browser_new_session" % sid, True)
    resp = be.call_tool(name, args)
    if "error" in resp:
        return text_result("backend error: " + json.dumps(resp["error"]), True)
    return resp.get("result", text_result("(no result)"))


# ---- JSON-RPC server loop --------------------------------------------------
OUT_LOCK = threading.Lock()


def reply(mid, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": mid}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    with OUT_LOCK:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def worker(req):
    mid = req.get("id")
    params = req.get("params") or {}
    try:
        reply(mid, handle_call(params.get("name"),
                               dict(params.get("arguments") or {})))
    except Exception as e:
        reply(mid, text_result("error: %s" % e, True))


def reaper():
    while True:
        time.sleep(60)
        try:
            POOL.reap_idle()
        except Exception as e:
            log("reaper: %s" % e)


def main():
    log("v%s starting; MAX=%d headless=%s idle_timeout=%ds state=%s"
        % (VERSION, MAX, HEADLESS, IDLE_TIMEOUT,
           "yes" if os.path.exists(STATE) else "MISSING (anonymous browsers)"))
    if IDLE_TIMEOUT > 0:
        threading.Thread(target=reaper, daemon=True).start()
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
        except Exception:
            continue
        method, mid = req.get("method"), req.get("id")
        if method == "initialize":
            reply(mid, {"protocolVersion": PROTO,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": "browserpool", "version": VERSION}})
        elif method == "notifications/initialized":
            pass  # notification, no reply
        elif method == "ping":
            reply(mid, {})
        elif method == "tools/list":
            try:
                reply(mid, {"tools": catalog()})
            except Exception as e:
                reply(mid, error={"code": -32603, "message": str(e)})
        elif method == "tools/call":
            threading.Thread(target=worker, args=(req,), daemon=True).start()
        elif mid is not None:
            reply(mid, error={"code": -32601,
                              "message": "method not found: %s" % method})
    POOL.close_all()


if __name__ == "__main__":
    main()
