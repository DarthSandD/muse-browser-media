"""Headless browser manager for automated Muse (video) generation.

Bursty model: launch only when a job needs it, then shut it down — so the
browser is not resident all day on this 7.75 GB box.

RAM guard: refuses to launch (and can auto-kill) when free RAM is below a
floor, because this machine runs at <0.5 GB free and a browser can push it
into heavy paging.

CLI:
  python3 muse_headless.py start     [--min-free 0.8]
  python3 muse_headless.py stop
  python3 muse_headless.py status
  python3 muse_headless.py ram
  python3 muse_headless.py guard     [--min-free 0.6]   # kill if below floor
"""
import argparse, json, os, subprocess, sys, time

# Browser binary and profile are configurable so the tool is portable.
#   BROWSER_BIN     — path to a Chromium-family browser (chrome/thorium/edge/brave)
#   BROWSER_PROFILE — the user-data-dir that holds the signed-in Muse session
BROWSER_BIN = os.environ.get(
    "BROWSER_BIN",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
)
PROFILE = os.environ.get("BROWSER_PROFILE", os.path.expanduser("~/.muse-browser-profile"))
PORT = int(os.environ.get("MUSE_CDP_PORT", "9333"))
LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_muse_headless.log")

# Memory-frugal flags. Verified: CDP up in ~3s, Muse SPA loads and is logged in.
FLAGS = [
    f"--remote-debugging-port={PORT}",
    "--remote-allow-origins=*",
    f"--user-data-dir={PROFILE}",
    "--headless=new",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-gpu",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-dev-shm-usage",
    "--disable-sync",
    "--disable-translate",
    "--no-service-autorun",
    "--renderer-process-limit=1",
    "--js-flags=--max-old-space-size=512",
    "--window-size=1920,1080",
    "https://muse.ai/",
]


def _ps(cmd):
    r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                       capture_output=True, text=True)
    return r.stdout.strip()


def free_ram_gb():
    try:
        return int(_ps("(Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory")) / 1024 / 1024
    except Exception:
        return -1.0


def _proc_name():
    """Process name for the browser, derived from the configured binary."""
    base = os.path.basename(BROWSER_BIN)
    return base[:-4] if base.lower().endswith(".exe") else base


def browser_ram_mb():
    try:
        out = _ps(f"$p=Get-Process {_proc_name()} -ErrorAction SilentlyContinue;"
                  "if($p){[math]::Round((($p|Measure-Object WorkingSet64 -Sum).Sum)/1MB,1)}else{0}")
        return float(out or 0)
    except Exception:
        return -1.0


def browser_count():
    try:
        return int(_ps(f"(Get-Process {_proc_name()} -ErrorAction SilentlyContinue).Count") or 0)
    except Exception:
        return 0


def cdp_up():
    r = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                        "--max-time", "5", f"http://127.0.0.1:{PORT}/json/version"],
                       capture_output=True, text=True)
    return r.stdout.strip() == "200"


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def stop():
    if browser_count() == 0:
        log("stop: browser not running")
        return True
    subprocess.run(["taskkill", "/F", "/IM", os.path.basename(BROWSER_BIN)],
                   capture_output=True, text=True)
    time.sleep(3)
    n = browser_count()
    log(f"stop: {_proc_name()} procs now {n}")
    return n == 0


def start(min_free=0.8, wait_s=60):
    if cdp_up():
        log(f"start: already up (free {free_ram_gb():.2f} GB, "
            f"{_proc_name()} {browser_ram_mb():.1f} MB)")
        return True

    free = free_ram_gb()
    if free < min_free:
        log(f"start: REFUSED — free RAM {free:.2f} GB < floor {min_free:.2f} GB")
        return False

    # a headed browser holds the profile lock; headless needs it free
    if browser_count() > 0:
        log(f"start: closing existing {_proc_name()} to free the profile lock")
        stop()
        time.sleep(2)

    log(f"start: launching headless (free RAM {free:.2f} GB)")
    subprocess.Popen([BROWSER_BIN] + FLAGS, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL,
                     creationflags=subprocess.CREATE_NO_WINDOW)

    deadline = time.time() + wait_s
    while time.time() < deadline:
        time.sleep(3)
        if cdp_up():
            time.sleep(6)  # let the SPA settle
            log(f"start: CDP up. free {free_ram_gb():.2f} GB, "
                f"{_proc_name()} {browser_ram_mb():.1f} MB")
            return True
        # abort if the box is being pushed into paging
        if free_ram_gb() < 0.25:
            log(f"start: ABORT — RAM collapsed to {free_ram_gb():.2f} GB")
            stop()
            return False
    log("start: FAILED — CDP never came up")
    return False


def status():
    up = cdp_up()
    free = free_ram_gb()
    tram = browser_ram_mb()
    n = browser_count()
    log(f"status: cdp={'UP' if up else 'DOWN'} procs={n} "
        f"{_proc_name()}={tram:.1f}MB free_ram={free:.2f}GB")
    if up:
        r = subprocess.run(["curl", "-s", "--max-time", "8",
                            f"http://127.0.0.1:{PORT}/json/list"],
                           capture_output=True, text=True)
        try:
            for t in json.loads(r.stdout):
                if t.get("type") == "page":
                    log(f"        tab: {t.get('title','')[:45]} | {t.get('url','')[:50]}")
        except Exception:
            pass
    return {"cdp": up, "procs": n, "browser_mb": tram, "free_gb": free}


def guard(min_free=0.6):
    """Kill headless Thorium if RAM has fallen below the floor."""
    free = free_ram_gb()
    if free < min_free and browser_count() > 0:
        log(f"guard: free RAM {free:.2f} GB < {min_free:.2f} GB — killing browser")
        stop()
        return False
    log(f"guard: ok (free {free:.2f} GB, floor {min_free:.2f} GB)")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["start", "stop", "status", "ram", "guard"])
    ap.add_argument("--min-free", type=float)
    a = ap.parse_args()

    if a.action == "start":
        sys.exit(0 if start(a.min_free if a.min_free is not None else 0.8) else 1)
    elif a.action == "stop":
        sys.exit(0 if stop() else 1)
    elif a.action == "status":
        status()
    elif a.action == "ram":
        print(json.dumps({"free_gb": free_ram_gb(),
                          "browser_mb": browser_ram_mb(),
                          "procs": browser_count()}, indent=1))
    elif a.action == "guard":
        guard(a.min_free if a.min_free is not None else 0.6)


if __name__ == "__main__":
    main()
