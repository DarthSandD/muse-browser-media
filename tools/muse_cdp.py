"""Minimal CDP client for the user's Thorium browser (port 9333).

Lets us drive the already-logged-in muse.ai tab without touching credentials.
"""
import json, subprocess, asyncio, sys

PORT = 9333

def list_tabs():
    r = subprocess.run(["curl", "-s", "--max-time", "10",
                        f"http://127.0.0.1:{PORT}/json/list"],
                       capture_output=True, text=True)
    return json.loads(r.stdout)

def find_tab(match="muse.ai"):
    for t in list_tabs():
        if t.get("type") == "page" and match in t.get("url", ""):
            return t
    return None

async def _call_many(ws_url, calls, timeout=30):
    """Send several CDP commands on ONE connection; return the last result."""
    import websockets
    async with websockets.connect(ws_url, max_size=128*1024*1024,
                                  open_timeout=15, close_timeout=5,
                                  ping_interval=None) as ws:
        last = None
        for i, (method, params) in enumerate(calls, 1):
            await ws.send(json.dumps({"id": i, "method": method, "params": params or {}}))
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
                if msg.get("id") == i:
                    last = msg
                    break
        return last

_ACTIVATED = False

def call(method, params=None, tab=None, timeout=30):
    global _ACTIVATED
    t = tab or find_tab()
    if not t:
        raise RuntimeError("no muse.ai tab found")
    calls = []
    if not _ACTIVATED:
        calls += [
            ("Page.enable", {}),
            ("Page.bringToFront", {}),
            ("Emulation.setFocusEmulationEnabled", {"enabled": True}),
            ("Emulation.setDeviceMetricsOverride",
             {"width": 1920, "height": 1080, "deviceScaleFactor": 1, "mobile": False}),
        ]
        _ACTIVATED = True
    calls.append((method, params or {}))
    return asyncio.run(_call_many(t["webSocketDebuggerUrl"], calls, timeout))

def js(expr, tab=None, timeout=30):
    r = call("Runtime.evaluate",
             {"expression": expr, "returnByValue": True, "awaitPromise": True},
             tab, timeout)
    res = r.get("result", {}).get("result", {})
    if "value" in res:
        return res["value"]
    return res

if __name__ == "__main__":
    t = find_tab()
    print("tab:", (t or {}).get("title"), "|", (t or {}).get("url"))
    print("title:", js("document.title"))
    print("metrics:", js("JSON.stringify({w:innerWidth,h:innerHeight})"))
