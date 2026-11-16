#!/usr/bin/env python3
"""
Remyy helper for macOS.

Serves remyy.html on http://localhost:8000 and gives it a small set of Mac actions.
Safety design:
  * binds to 127.0.0.1 only
  * every launch makes a random token that only the page it serves knows
  * Host and Origin headers are checked (blocks other websites from calling it)
  * only the fixed actions below exist: no shell, no arbitrary commands or scripts
  * calendar, notes and screen capture are fixed actions too (no shell)
  * /api/stats reports CPU, memory, disk, thermal state and battery for the HUD
  * Remyy's memory is a plain JSON file at ~/.remyy/memory.json (you can read or edit it)
Run:  python3 remyy_server.py            (optional: --port 8000 --no-browser)
"""
import urllib.error, argparse, base64, ctypes, ctypes.util, hmac, json, os, re, secrets, shutil, subprocess, sys, tempfile, threading, time, urllib.request, webbrowser
from html import escape as html_escape
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOKEN = secrets.token_urlsafe(24)
PORT = 8000

MEM_DIR = Path.home() / ".remyy"
MEM_FILE = MEM_DIR / "memory.json"
APP_RE = re.compile(r"[\w .&+\-()']{1,60}")


# ---------------- memory file ----------------
def clean_facts(facts):
    out = []
    for f in (facts[:200] if isinstance(facts, list) else []):
        if isinstance(f, dict) and isinstance(f.get("text"), str) and f["text"].strip():
            ts = f.get("ts", 0)
            out.append({
                "id": (str(f.get("id", ""))[:40] or secrets.token_hex(4)),
                "text": f["text"].strip()[:300],
                "ts": int(ts) if isinstance(ts, (int, float)) else 0,
            })
    return out


def mem_load():
    try:
        data = json.loads(MEM_FILE.read_text(encoding="utf-8"))
        return clean_facts(data.get("facts", []) if isinstance(data, dict) else [])
    except Exception:
        return []


def mem_save(facts):
    MEM_DIR.mkdir(mode=0o700, exist_ok=True)
    tmp = MEM_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"facts": clean_facts(facts)}, indent=2, ensure_ascii=False), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(MEM_FILE)


# ---------------- system stats ----------------
_cache = {}
_cpu_prev = None
_cpu_lock = threading.Lock()
_libc = None


def cached(key, ttl, fn):
    """Run fn at most once per ttl seconds (for slower commands)."""
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    try:
        val = fn()
    except Exception:
        val = None
    _cache[key] = (now, val)
    return val


def _cpu_ticks():
    """user, system, idle, nice tick counters from the macOS kernel."""
    global _libc
    if _libc is None:
        lib = ctypes.CDLL(ctypes.util.find_library("c") or "/usr/lib/libSystem.B.dylib")

        class Info(ctypes.Structure):
            _fields_ = [("cpu_ticks", ctypes.c_uint32 * 4)]

        lib.mach_host_self.restype = ctypes.c_uint32
        lib.host_statistics.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.POINTER(Info), ctypes.POINTER(ctypes.c_uint32)]
        lib.host_statistics.restype = ctypes.c_int
        _libc = (lib, Info)
    lib, Info = _libc
    info, count = Info(), ctypes.c_uint32(4)
    if lib.host_statistics(lib.mach_host_self(), 3, ctypes.byref(info), ctypes.byref(count)) != 0:
        raise RuntimeError("host_statistics failed")
    return list(info.cpu_ticks)


def cpu_stats():
    global _cpu_prev
    with _cpu_lock:
        cur = _cpu_ticks()
        if _cpu_prev is None:
            time.sleep(0.25)
            prev, cur = cur, _cpu_ticks()
        else:
            prev = _cpu_prev
        _cpu_prev = cur
    d = [(c - p) % 2**32 for c, p in zip(cur, prev)]
    total = sum(d)
    return {"cpu_percent": round(100.0 * (total - d[2]) / total, 1) if total else 0.0}


def parse_vm_stat(text, memsize):
    page = int(re.search(r"page size of (\d+) bytes", text).group(1))

    def pages(name):
        m = re.search(name + r":\s+(\d+)", text)
        return int(m.group(1)) if m else 0

    used = (pages("Pages active") + pages("Pages wired down") + pages("Pages occupied by compressor")) * page
    return {
        "ram_percent": round(100.0 * used / memsize, 1),
        "ram_used_gb": round(used / 2**30, 1),
        "ram_total_gb": round(memsize / 2**30, 1),
    }


def mem_stats():
    total = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5).stdout.strip())
    out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
    return parse_vm_stat(out, total)


def disk_stats():
    u = shutil.disk_usage("/")
    return {
        "disk_percent": round(100.0 * (u.total - u.free) / u.total, 1),
        "disk_free_gb": round(u.free / 10**9),
        "disk_total_gb": round(u.total / 10**9),
    }


def thermal_stats():
    def state():
        r = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", 'ObjC.import("Foundation"); $.NSProcessInfo.processInfo.thermalState'],
            capture_output=True, text=True, timeout=5)
        return ["Nominal", "Fair", "Serious", "Critical"][int(r.stdout.strip())]

    out = {}
    s = cached("thermal", 8, state)
    if s:
        out["thermal"] = s
    # Optional: real degrees C if the third-party "smctemp" tool is installed
    if shutil.which("smctemp"):
        def deg():
            r = subprocess.run(["smctemp", "-c"], capture_output=True, text=True, timeout=4)
            return float(re.search(r"\d+(?:\.\d+)?", r.stdout).group(0))
        c = cached("cputemp", 5, deg)
        if c:
            out["cpu_temp_c"] = round(c)
    return out


ISTAT_DEFAULT = "http://localhost:4027/api/"
_istat = {"t": 0.0, "data": {}, "retry_at": 0.0}


def istat_config_url():
    """Optional override in ~/.remyy/config.json: {"istatistica_url": "http://localhost:4027/api/"}"""
    try:
        cfg = json.loads((MEM_DIR / "config.json").read_text(encoding="utf-8"))
        url = str(cfg.get("istatistica_url", "")).strip()
        if re.fullmatch(r"https?://[^\s]{1,300}", url):
            return url
    except Exception:
        pass
    return ISTAT_DEFAULT


def istat_fetch(url):
    # The iStatistica web server sometimes answers "Access Denied" (HTML), so retry a few times.
    for _ in range(4):
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                return json.loads(r.read(3_000_000))
        except ValueError:
            time.sleep(0.15)
        except Exception:
            return None
    return None


def istat_stats():
    """Real temperatures and fan speed from iStatistica Pro's web access (if it is running)."""
    now = time.time()
    if now < _istat["retry_at"]:
        return {}
    if now - _istat["t"] < 1.5:
        return _istat["data"]
    d = istat_fetch(istat_config_url())
    sensors = d.get("sensors") if isinstance(d, dict) else None
    if not isinstance(sensors, dict) or not sensors:
        _istat["retry_at"] = now + 20      # not running or not enabled: stop asking for a while
        _istat["data"] = {}
        return {}
    temps = {}
    for name, v in sensors.items():
        try:
            temps[str(name)] = float(v)    # the API reports degrees Celsius
        except (TypeError, ValueError):
            pass
    out = {}
    cpu = [v for n, v in temps.items() if "cpu" in n.lower()] or [v for n, v in temps.items() if "soc" in n.lower()]
    if cpu:
        out["cpu_temp_c"] = round(max(cpu))
    gpu = [v for n, v in temps.items() if "gpu" in n.lower()]
    if gpu:
        out["gpu_temp_c"] = round(max(gpu))
    ssd = [v for n, v in temps.items() if "ssd" in n.lower() or "nand" in n.lower()]
    if ssd:
        out["ssd_temp_c"] = round(max(ssd))
    fans = d.get("fans")
    if isinstance(fans, dict):
        rpms = []
        for v in fans.values():
            try:
                rpms.append(float(v))
            except (TypeError, ValueError):
                pass
        if rpms:
            out["fan_rpm"] = round(max(rpms))
    if out:
        out["temp_source"] = "iStatistica"
    _istat["t"], _istat["data"] = now, out
    return out


def battery_stats():
    def batt():
        text = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=5).stdout
        m = re.search(r"(\d+)%;\s*([\w ]+)", text)
        return {"battery_percent": int(m.group(1)), "battery_state": m.group(2).strip()} if m else {}
    return cached("battery", 10, batt) or {}


def collect_stats():
    s = {"ok": True}
    for fn in (cpu_stats, mem_stats, disk_stats, thermal_stats, istat_stats, battery_stats):
        try:
            s.update(fn())
        except Exception:
            pass
    return s


def osa(*lines, args=(), lang=None, timeout=10):
    cmd = ["osascript"] + (["-l", lang] if lang else [])
    for line in lines:
        cmd += ["-e", line]
    cmd += [str(a) for a in args]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    out = (r.stdout or "").strip()
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "AppleScript failed").strip()[:300])
    return out


def app_name(a):
    n = str(a.get("name", "")).strip()
    if not APP_RE.fullmatch(n):
        raise ValueError("Invalid application name")
    return n


# ---------------- actions ----------------
def open_app(a):
    n = app_name(a)
    r = subprocess.run(["open", "-a", n], capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        return {"ok": False, "error": f"Could not find or open {n}"}
    return {"ok": True, "opened": n}


def quit_app(a):
    n = app_name(a)
    osa("on run argv", "tell application (item 1 of argv) to quit", "end run", args=[n])
    return {"ok": True, "quit": n}


def set_volume(a):
    level = max(0, min(100, int(float(a.get("level", 50)))))
    osa(f"set volume output volume {level}")
    return {"ok": True, "volume": level}


def set_mute(a):
    muted = bool(a.get("muted", True))
    osa(f"set volume output muted {'true' if muted else 'false'}")
    return {"ok": True, "muted": muted}


def media_control(a):
    app = a.get("app")
    cmd = a.get("command")
    if app not in ("Music", "Spotify"):
        raise ValueError("app must be Music or Spotify")
    verbs = {"play_pause": "playpause", "next": "next track", "previous": "previous track"}
    if cmd in verbs:
        osa(f'tell application "{app}" to {verbs[cmd]}')
        return {"ok": True, "done": cmd}
    if cmd == "now_playing":
        out = osa(
            f'tell application "{app}"',
            "if player state is playing then",
            'return (name of current track) & " by " & (artist of current track)',
            "else",
            'return "Nothing is playing"',
            "end if",
            "end tell",
        )
        return {"ok": True, "now_playing": out}
    raise ValueError("Unknown media command")


def now_playing(a):
    """Read-only: report what Spotify or Music is playing, without launching either app."""
    found = None
    for app in ("Spotify", "Music"):
        out = osa(
            'tell application "System Events" to set up to (name of every process) contains "%s"' % app,
            'if up is false then return ""',
            'tell application "%s"' % app,
            'if player state is stopped then return ""',
            'set st to "paused"',
            'if player state is playing then set st to "playing"',
            'return (name of current track) & (ASCII character 31) & (artist of current track) & (ASCII character 31) & st',
            "end tell",
        )
        if out:
            parts = out.split("\x1f")
            if len(parts) >= 3:
                cur = {"ok": True, "player": app, "title": parts[0][:120], "artist": parts[1][:120], "state": parts[2]}
                if cur["state"] == "playing":
                    return cur
                found = found or cur
    return found or {"ok": True, "player": "", "title": "", "artist": "", "state": "none"}


def open_url(a):
    url = str(a.get("url", "")).strip()
    if not re.fullmatch(r"https?://[^\s]{1,2000}", url):
        raise ValueError("Only http(s) URLs are allowed")
    subprocess.run(["open", url], timeout=10)
    return {"ok": True, "opened": url}


def get_mac_status(_a):
    info = {"ok": True, "time": datetime.now().strftime("%A %H:%M, %d %B %Y")}
    try:
        batt = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=5).stdout
        m = re.search(r"(\d+)%;\s*([\w ]+)", batt)
        if m:
            info["battery_percent"] = int(m.group(1))
            info["battery_state"] = m.group(2).strip()
    except Exception:
        pass
    info.update({k: v for k, v in collect_stats().items() if k != "ok"})
    for key, script in (
        ("volume", "output volume of (get volume settings)"),
        ("muted", "output muted of (get volume settings)"),
        ("frontmost_app", 'tell application "System Events" to get name of first application process whose frontmost is true'),
    ):
        try:
            info[key] = osa(script)
        except Exception:
            pass
    return info


def sleep_display(_a):
    subprocess.run(["pmset", "displaysleepnow"], timeout=5)
    return {"ok": True}


def show_notification(a):
    msg = str(a.get("message", ""))[:200]
    osa("on run argv", 'display notification (item 1 of argv) with title "Remyy"', "end run", args=[msg])
    return {"ok": True}


# ---------------- screen, calendar, notes and system controls ----------------
RS = "\x1e"   # record separator used in AppleScript output
US = "\x1f"   # field separator


def as_lines(script):
    return [ln for ln in script.strip("\n").split("\n") if ln.strip()]


def safe_arg(s):
    """osascript would read an argument starting with '-' as an option."""
    s = str(s)
    return " " + s if s.startswith("-") else s


AS_HELPERS = """
on fmt(d)
  set y to year of d
  set m to (month of d) as integer
  set dy to day of d
  set h to hours of d
  set mi to minutes of d
  return (y as text) & "-" & my pad(m) & "-" & my pad(dy) & " " & my pad(h) & ":" & my pad(mi)
end fmt
on pad(n)
  if n < 10 then return "0" & (n as text)
  return n as text
end pad
"""

CAL_LIST = """
on run argv
  set y to (item 1 of argv) as integer
  set mo to (item 2 of argv) as integer
  set dd to (item 3 of argv) as integer
  set n to (item 4 of argv) as integer
  set d1 to current date
  set day of d1 to 1
  set year of d1 to y
  set month of d1 to mo
  set day of d1 to dd
  set time of d1 to 0
  set d2 to d1 + (n * days)
  set rs to (ASCII character 30)
  set us to (ASCII character 31)
  set out to ""
  tell application "Calendar"
    repeat with c in calendars
      try
        set cname to name of c
        set evs to (every event of c whose start date < d2 and end date > d1)
        repeat with e in evs
          try
            set loc to ""
            try
              set loc to location of e
              if loc is missing value then set loc to ""
            end try
            set out to out & (summary of e) & us & my fmt(start date of e) & us & my fmt(end date of e) & us & ((allday event of e) as text) & us & cname & us & loc & rs
          end try
        end repeat
      end try
    end repeat
  end tell
  return out
end run
"""

CAL_ADD = """
on run argv
  set t to item 1 of argv
  set y to (item 2 of argv) as integer
  set mo to (item 3 of argv) as integer
  set dd to (item 4 of argv) as integer
  set hh to (item 5 of argv) as integer
  set mi to (item 6 of argv) as integer
  set dur to (item 7 of argv) as integer
  set calName to item 8 of argv
  set allDay to ((item 9 of argv) is "true")
  set sd to current date
  set day of sd to 1
  set year of sd to y
  set month of sd to mo
  set day of sd to dd
  set time of sd to (hh * 3600 + mi * 60)
  if allDay then
    set time of sd to 0
    set ed to sd + (1 * days)
  else
    set ed to sd + (dur * minutes)
  end if
  tell application "Calendar"
    set tcal to missing value
    if calName is not "" then
      try
        set tcal to first calendar whose name is calName
      end try
    end if
    if tcal is missing value then
      repeat with c in calendars
        try
          if writable of c then
            set tcal to c
            exit repeat
          end if
        end try
      end repeat
    end if
    if tcal is missing value then error "No writable calendar found"
    make new event at end of events of tcal with properties {summary:t, start date:sd, end date:ed, allday event:allDay}
    return name of tcal
  end tell
end run
"""

NOTE_ADD = """
on run argv
  tell application "Notes"
    make new note with properties {body:(item 1 of argv)}
  end tell
  return "ok"
end run
"""

NOTE_FIND = """
on run argv
  set q to item 1 of argv
  set rs to (ASCII character 30)
  set us to (ASCII character 31)
  set out to ""
  set cnt to 0
  tell application "Notes"
    set hits to (every note whose name contains q or plaintext contains q)
    repeat with nt in hits
      set cnt to cnt + 1
      if cnt > 5 then exit repeat
      try
        set p to plaintext of nt
        if (length of p) > 600 then set p to text 1 thru 600 of p
        set fld to ""
        try
          set fld to name of container of nt
        end try
        set out to out & (name of nt) & us & fld & us & my fmt(modification date of nt) & us & p & rs
      end try
    end repeat
  end tell
  return out
end run
"""

WINDOW_JS = """function run(argv) {
  ObjC.import('CoreGraphics');
  var want = (argv[0] || '').toLowerCase();
  var arr = ObjC.deepUnwrap(ObjC.castRefToObject($.CGWindowListCopyWindowInfo(17, 0)));
  for (var i = 0; i < arr.length; i++) {
    var w = arr[i];
    if (w.kCGWindowLayer !== 0) continue;
    if ((w.kCGWindowOwnerName || '').toLowerCase().indexOf(want) < 0) continue;
    if ((w.kCGWindowName || '') === 'Remyy') continue;
    var b = w.kCGWindowBounds;
    if (!b || b.Width < 100 || b.Height < 100) continue;
    return String(w.kCGWindowNumber);
  }
  return '';
}"""


def run_as(script, args=(), timeout=40):
    return osa(*as_lines(script + AS_HELPERS), args=[safe_arg(a) for a in args], timeout=timeout)


def parse_records(out, nfields):
    rows = []
    for rec in out.split(RS):
        if not rec.strip():
            continue
        f = rec.split(US)
        f += [""] * (nfields - len(f))
        rows.append([x.strip("\n") for x in f[:nfields]])
    return rows


def parse_dt(s):
    s = str(s).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:16] if fmt.endswith("%M") else s[:10], fmt)
        except ValueError:
            continue
    raise ValueError("Use the format YYYY-MM-DD HH:MM")


# ----- screen -----
def capture_screen(a):
    app = str(a.get("app", "")).strip()
    tmp = tempfile.mkdtemp(prefix="remyy-shot-")
    try:
        if app:
            if not APP_RE.fullmatch(app):
                raise ValueError("Invalid application name")
            wid = osa(WINDOW_JS, args=[safe_arg(app)], lang="JavaScript")
            if not wid.isdigit():
                return {"ok": False, "error": f"I can't find a visible window for {app}."}
            p = os.path.join(tmp, "window.jpg")
            subprocess.run(["screencapture", "-x", "-o", "-t", "jpg", "-l", wid, p], timeout=15, check=True)
            paths = [p]
        else:
            targets = [os.path.join(tmp, f"display{i + 1}.jpg") for i in range(3)]
            subprocess.run(["screencapture", "-x", "-t", "jpg"] + targets, timeout=15, check=True)
            paths = [p for p in targets if os.path.exists(p)]
        images = []
        for p in paths:
            subprocess.run(["sips", "-Z", "1568", p], capture_output=True, timeout=15)
            with open(p, "rb") as fh:
                data = fh.read()
            if 0 < len(data) < 4_000_000:
                images.append(base64.b64encode(data).decode())
        if not images:
            return {"ok": False, "error": "Screen capture failed. Allow Screen Recording for Remyy in System Settings, Privacy and Security."}
        return {"ok": True, "images": images, "count": len(images)}
    except subprocess.CalledProcessError:
        return {"ok": False, "error": "Screen capture failed. Allow Screen Recording for Remyy in System Settings, Privacy and Security."}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ----- calendar -----
def list_calendar_events(a):
    day = str(a.get("date") or datetime.now().strftime("%Y-%m-%d"))
    d = parse_dt(day)
    n = max(1, min(14, int(float(a.get("days") or 1))))
    out = run_as(CAL_LIST, [d.year, d.month, d.day, n], timeout=45)
    events = [
        {"title": r[0], "start": r[1], "end": r[2], "all_day": r[3] == "true", "calendar": r[4], "location": r[5]}
        for r in parse_records(out, 6)
    ]
    events.sort(key=lambda e: e["start"])
    return {"ok": True, "count": len(events), "events": events[:40]}


def add_calendar_event(a):
    title = str(a.get("title", "")).strip()[:200]
    if not title:
        raise ValueError("An event needs a title")
    start = parse_dt(a.get("start", ""))
    minutes = max(5, min(1440, int(float(a.get("minutes") or 60))))
    all_day = bool(a.get("all_day", False))
    cal = str(a.get("calendar", "")).strip()[:60]
    used = run_as(CAL_ADD, [title, start.year, start.month, start.day, start.hour, start.minute,
                            minutes, cal, "true" if all_day else "false"], timeout=30)
    return {"ok": True, "added": title, "calendar": used, "start": start.strftime("%Y-%m-%d %H:%M"),
            "minutes": minutes, "all_day": all_day}


# ----- notes -----
def add_note(a):
    title = str(a.get("title", "")).strip()[:200]
    body = str(a.get("body", "")).strip()[:5000]
    if not title:
        raise ValueError("A note needs a title")
    html = "<div><h1>" + html_escape(title) + "</h1></div>"
    if body:
        html += "<div>" + html_escape(body).replace("\n", "<br>") + "</div>"
    run_as(NOTE_ADD, [html], timeout=30)
    return {"ok": True, "added": title}


def find_notes(a):
    q = str(a.get("query", "")).strip()[:80]
    if len(q) < 2:
        raise ValueError("Give a word or two to search for")
    out = run_as(NOTE_FIND, [q], timeout=45)
    notes = [{"title": r[0], "folder": r[1], "modified": r[2], "text": r[3]} for r in parse_records(out, 4)]
    return {"ok": True, "count": len(notes), "notes": notes}


def news_search(a):
    """Headlines from Google News RSS (fixed host, no key). Used because Gemini search grounding is not free."""
    import urllib.parse, xml.etree.ElementTree as ET
    q = str((a or {}).get("query", "")).strip()[:200]
    if not q:
        return {"ok": False, "error": "empty query"}
    url = "https://news.google.com/rss/search?q=" + urllib.parse.quote(q) + "&hl=en-US&gl=US&ceid=US:en"
    try:
        root = ET.fromstring(http_get(url))
    except HttpFail as e:
        return {"ok": False, "error": "Google News error: " + (e.why or str(e.code))}
    except Exception:
        return {"ok": False, "error": "Google News returned something unexpected"}
    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        src = (it.findtext("source") or "").strip()
        if src and title.endswith(" - " + src):
            title = title[: -len(src) - 3]
        if title and link.startswith("https://"):
            items.append({"title": title[:200], "source": src[:60], "link": link, "published": (it.findtext("pubDate") or "")[:31]})
        if len(items) >= 8:
            break
    return {"ok": True, "count": len(items), "items": items}


class HttpFail(Exception):
    def __init__(self, code=0, why=""):
        super().__init__(why or str(code)); self.code = code; self.why = why


def http_get(url, timeout=8):
    """GET a fixed https URL. Tries Python first; if Python cannot verify certificates (common with python.org
    installs on macOS) falls back to the system curl, which uses the macOS certificate store."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Remyy)", "Accept": "application/json, application/xml, */*"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(2_000_000)
    except urllib.error.HTTPError as e:
        raise HttpFail(e.code, "HTTP " + str(e.code))
    except Exception as e:
        why = type(e).__name__ + ": " + str(getattr(e, "reason", e))[:80]
        if "CERTIFICATE" not in why.upper() and "SSL" not in why.upper():
            raise HttpFail(0, why)
    # certificate problem in Python: retry with curl (the URL goes in on stdin, so a key is not visible in the process list)
    try:
        cfg = 'url = "' + url.replace("\\", "").replace('"', "") + '"\nuser-agent = "Mozilla/5.0 (Remyy)"\n'
        p = subprocess.run(["/usr/bin/curl", "-sS", "-L", "-m", str(timeout), "-K", "-", "-w", "\n%{http_code}"],
                           input=cfg.encode(), capture_output=True, timeout=timeout + 3)
        body, _, code = p.stdout.rpartition(b"\n")
        code = int(code or 0)
        if p.returncode != 0 or code == 0:
            raise HttpFail(0, "curl: " + p.stderr.decode("utf-8", "replace").strip()[:80])
        if code >= 400:
            raise HttpFail(code, "HTTP " + str(code))
        return body[:2_000_000]
    except HttpFail:
        raise
    except Exception as e:
        raise HttpFail(0, "curl failed: " + type(e).__name__)


KEY_RE = re.compile(r"[A-Za-z0-9_\-]{8,100}")


def _get_json(url, label):
    try:
        return json.loads(http_get(url).decode("utf-8", "replace")), None
    except HttpFail as e:
        hint = " (check the key)" if e.code in (401, 403) else " (limit reached)" if e.code == 429 else ""
        return None, label + " error: " + (e.why or str(e.code)) + hint
    except ValueError:
        return None, label + " returned something unexpected"


def guardian_search(a):
    """The Guardian Open Platform (free developer key, fixed host)."""
    import urllib.parse
    q = str((a or {}).get("query", "")).strip()[:200]
    key = str((a or {}).get("key", "")).strip()
    if not q or not KEY_RE.fullmatch(key):
        return {"ok": False, "error": "Guardian: missing query or key"}
    url = ("https://content.guardianapis.com/search?q=" + urllib.parse.quote(q)
           + "&order-by=relevance&page-size=5&show-fields=trailText&api-key=" + key)
    j, err = _get_json(url, "Guardian")
    if err:
        return {"ok": False, "error": err}
    items = []
    for r in ((j or {}).get("response") or {}).get("results", [])[:5]:
        link = str(r.get("webUrl", ""))
        if link.startswith("https://"):
            summ = re.sub(r"<[^>]+>", "", str((r.get("fields") or {}).get("trailText", "")))
            items.append({"title": str(r.get("webTitle", ""))[:200], "source": "The Guardian", "link": link,
                          "published": str(r.get("webPublicationDate", ""))[:20], "summary": summ[:300]})
    return {"ok": True, "items": items}


def newsdata_search(a):
    """NewsData.io (free key, fixed host)."""
    import urllib.parse
    q = str((a or {}).get("query", "")).strip()[:100]
    key = str((a or {}).get("key", "")).strip()
    if not q or not KEY_RE.fullmatch(key):
        return {"ok": False, "error": "NewsData: missing query or key"}
    url = "https://newsdata.io/api/1/latest?language=en&q=" + urllib.parse.quote(q) + "&apikey=" + key
    j, err = _get_json(url, "NewsData")
    if err:
        return {"ok": False, "error": err}
    if (j or {}).get("status") != "success":
        return {"ok": False, "error": "NewsData: " + str(((j or {}).get("results") or {}).get("message", "request refused"))[:100]}
    items = []
    for r in (j.get("results") or [])[:6]:
        link = str(r.get("link", ""))
        if link.startswith("https://"):
            items.append({"title": str(r.get("title", ""))[:200], "source": str(r.get("source_name") or r.get("source_id") or "")[:60],
                          "link": link, "published": str(r.get("pubDate", ""))[:20], "summary": str(r.get("description") or "")[:300]})
    return {"ok": True, "items": items}


ACTIONS = {
    "guardian_search": guardian_search,
    "newsdata_search": newsdata_search,
    "news_search": news_search,
    "open_app": open_app,
    "quit_app": quit_app,
    "set_volume": set_volume,
    "set_mute": set_mute,
    "media_control": media_control,
    "open_url": open_url,
    "get_mac_status": get_mac_status,
    "sleep_display": sleep_display,
    "show_notification": show_notification,
    "capture_screen": capture_screen,
    "list_calendar_events": list_calendar_events,
    "now_playing": now_playing,
    "add_calendar_event": add_calendar_event,
    "find_notes": find_notes,
    "add_note": add_note,
}


# ---------------- http ----------------
class Handler(BaseHTTPRequestHandler):
    server_version = "RemyyHelper"

    def log_message(self, fmt, *args):  # keep terminal quiet; show actions only
        pass

    def _allowed_hosts(self):
        return {f"localhost:{PORT}", f"127.0.0.1:{PORT}"}

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.headers.get("Host", "") not in self._allowed_hosts():
            return self._send(403, '{"ok":false,"error":"bad host"}')
        if self.path.split("?")[0] in ("/", "/remyy.html"):
            html = (HERE / "remyy.html").read_text(encoding="utf-8").replace("__REMYY_TOKEN__", TOKEN)
            return self._send(200, html, "text/html; charset=utf-8")
        self._send(404, '{"ok":false,"error":"not found"}')

    def do_POST(self):
        if self.path not in ("/api/mac", "/api/memory", "/api/stats"):
            return self._send(404, '{"ok":false,"error":"not found"}')
        if self.headers.get("Host", "") not in self._allowed_hosts():
            return self._send(403, '{"ok":false,"error":"bad host"}')
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}"}:
            return self._send(403, '{"ok":false,"error":"bad origin"}')
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n > (131072 if self.path == "/api/memory" else 8192):
                raise ValueError("too large")
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, '{"ok":false,"error":"bad request"}')
        if not hmac.compare_digest(str(req.get("token", "")), TOKEN):
            return self._send(403, '{"ok":false,"error":"bad token"}')

        if self.path == "/api/stats":
            return self._send(200, json.dumps(collect_stats()))

        if self.path == "/api/memory":
            try:
                if req.get("op") == "load":
                    return self._send(200, json.dumps({"ok": True, "facts": mem_load()}))
                if req.get("op") == "save":
                    mem_save(req.get("facts"))
                    return self._send(200, '{"ok":true}')
            except Exception as e:
                return self._send(500, json.dumps({"ok": False, "error": str(e)[:200]}))
            return self._send(400, '{"ok":false,"error":"unknown op"}')

        fn = ACTIONS.get(req.get("action"))
        if not fn:
            return self._send(400, '{"ok":false,"error":"unknown action"}')
        args = req.get("args") if isinstance(req.get("args"), dict) else {}
        try:
            result = fn(args)
            print(f"  {datetime.now():%H:%M:%S}  {req['action']} {json.dumps(args)[:100]}")
        except Exception as e:
            result = {"ok": False, "error": str(e)[:300]}
            print(f"  {datetime.now():%H:%M:%S}  {req['action']} FAILED: {e}")
        self._send(200, json.dumps(result))


def main():
    global PORT
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-browser", action="store_true")
    PORT = ap.parse_args().port
    no_browser = ap.parse_args().no_browser
    if not (HERE / "remyy.html").exists():
        sys.exit("remyy.html must be in the same folder as remyy_server.py")
    url = f"http://localhost:{PORT}/remyy.html"
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Remyy helper running at {url}\nPress Ctrl+C to stop.\n")
    if not no_browser:
        r = subprocess.run(["open", "-a", "Google Chrome", url], capture_output=True)
        if r.returncode != 0:
            webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
