"""Reusable browser-based media generation via the logged-in Thorium (CDP 9333).

Two routes, both driving a real logged-in browser — no API keys, no credits:
  * video -> Muse (muse.ai), Meta's agent. Generates a real 1080p MP4.
  * image -> Gemini (gemini.google.com). Generates a still.

CLI:
  python3 browser_gen.py video "prompt" [--out path.mp4] [--timeout 1800]
  python3 browser_gen.py image "prompt" [--out path.png] [--timeout 300]
  python3 browser_gen.py status

Requires: Thorium running with --remote-debugging-port=9333 and the target
site signed in inside that profile. Use `status` to check both.
"""
import argparse, asyncio, base64, json, os, subprocess, sys, time

PORT = 9333
MUSE_URL = "https://muse.ai/"
GEMINI_URL = "https://gemini.google.com/app"


# ---------------------------------------------------------------- CDP plumbing

def list_tabs():
    r = subprocess.run(["curl", "-s", "--max-time", "10",
                        f"http://127.0.0.1:{PORT}/json/list"],
                       capture_output=True, text=True)
    if not r.stdout.strip():
        raise RuntimeError("CDP not responding on 9333 — Thorium closed or wedged. "
                           "Relaunch it with the command in muse-ai-clip-route.md.")
    return json.loads(r.stdout)


def find_tab(substr):
    for t in list_tabs():
        if t.get("type") == "page" and substr in t.get("url", ""):
            return t
    return None


class Tab:
    """One persistent CDP connection. Fresh connections per command are flaky."""

    def __init__(self, tab, timeout=60):
        self.tab = tab
        self.timeout = timeout

    async def __aenter__(self):
        import websockets
        self.ws = await websockets.connect(self.tab["webSocketDebuggerUrl"],
                                           max_size=256 * 1024 * 1024,
                                           ping_interval=None, open_timeout=15)
        self._id = 0
        await self.cmd("Page.enable")
        await self.cmd("Runtime.enable")
        return self

    async def __aexit__(self, *exc):
        try:
            await self.ws.close()
        except Exception:
            pass

    async def cmd(self, method, params=None, timeout=None):
        self._id += 1
        mid = self._id
        await self.ws.send(json.dumps({"id": mid, "method": method,
                                       "params": params or {}}))
        while True:
            msg = json.loads(await asyncio.wait_for(self.ws.recv(),
                                                    timeout=timeout or self.timeout))
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"CDP {method}: {msg['error']}")
                return msg.get("result", {})

    async def ev(self, expr, timeout=None):
        r = await self.cmd("Runtime.evaluate",
                           {"expression": expr, "returnByValue": True,
                            "awaitPromise": True}, timeout)
        inner = r.get("result", {})
        return inner.get("value", inner)

    async def click(self, x, y):
        for t in ("mousePressed", "mouseReleased"):
            await self.cmd("Input.dispatchMouseEvent",
                           {"type": t, "x": x, "y": y, "button": "left",
                            "clickCount": 1, "buttons": 1})

    async def type_into(self, selector_js, text):
        """Clear a React-controlled field, focus it, then insert text."""
        ok = await self.ev(f"""(() => {{
            const el = {selector_js};
            if (!el) return 'NOT_FOUND';
            const proto = el.tagName === 'TEXTAREA'
                ? window.HTMLTextAreaElement.prototype
                : window.HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
            if (setter) {{
                setter.call(el, '');
                el.dispatchEvent(new Event('input', {{bubbles: true}}));
            }} else {{
                el.textContent = '';
                el.dispatchEvent(new InputEvent('input', {{bubbles: true}}));
            }}
            el.focus();
            return 'OK';
        }})()""")
        if ok == "NOT_FOUND":
            raise RuntimeError("composer not found")
        await asyncio.sleep(0.5)
        await self.cmd("Input.insertText", {"text": text})
        await asyncio.sleep(0.7)

    async def send_via(self, aria_label):
        """Find a Send button by aria-label and click it. Returns True if clicked."""
        info = json.loads(await self.ev(f"""(() => {{
            const b = [...document.querySelectorAll('button')]
                .find(x => x.getAttribute('aria-label') === {json.dumps(aria_label)} && !x.disabled);
            if (!b) return JSON.stringify({{found: false}});
            const r = b.getBoundingClientRect();
            return JSON.stringify({{found: true,
                x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2)}});
        }})()"""))
        if not info.get("found"):
            return False
        await self.click(info["x"], info["y"])
        return True


# ------------------------------------------------------------------- Muse video

def muse_video(prompt, out_path, timeout_s=1800, poll_s=20):
    tab = find_tab("muse.ai")
    if not tab:
        raise RuntimeError("no muse.ai tab open in the debug browser")

    async def run():
        async with Tab(tab, timeout=60) as t:
            await t.cmd("Page.bringToFront")
            await t.cmd("Emulation.setFocusEmulationEnabled", {"enabled": True})

            # login gate
            wall = await t.ev("document.body.innerText.toLowerCase()"
                              ".includes('log in or create an account')")
            if wall:
                raise RuntimeError("Muse is NOT signed in in this browser profile")

            # how many .mp4 links exist before we ask (so we can spot the new one)
            before = json.loads(await t.ev(
                "JSON.stringify([...document.querySelectorAll('a[href]')]"
                ".filter(a=>/\\.mp4/i.test(a.href)).map(a=>a.href))"))

            await t.type_into(
                "document.querySelector('textarea[placeholder=\"Message\"]')", prompt)
            ta_len = await t.ev("document.querySelector('textarea[placeholder=\"Message\"]')"
                                ".value.length")
            print(f"[muse] typed {ta_len} chars", flush=True)
            if not await t.send_via("Send"):
                raise RuntimeError("Send button not found")
            await asyncio.sleep(2)
            left = await t.ev("document.querySelector('textarea[placeholder=\"Message\"]')"
                              ".value.length")
            if left != 0:
                raise RuntimeError("message did not send (composer not cleared)")
            print("[muse] sent; polling for the render (typically 15-25 min)...", flush=True)

            deadline = time.time() + timeout_s
            approved = False
            while time.time() < deadline:
                await asyncio.sleep(poll_s)

                # (a) Muse gates the public link behind an Allow/Deny dialog.
                #     Auto-approve, otherwise no <a href> link is ever emitted.
                if not approved:
                    hit = json.loads(await t.ev("""(() => {
                        const b = [...document.querySelectorAll('button')]
                            .find(x => (x.innerText||'').trim() === 'Allow'
                                    && !x.disabled && (x.offsetWidth||x.offsetHeight));
                        if (!b) return JSON.stringify({found:false});
                        const r = b.getBoundingClientRect();
                        return JSON.stringify({found:true,
                            x: Math.round(r.x+r.width/2), y: Math.round(r.y+r.height/2)});
                    })()"""))
                    if hit.get("found"):
                        await t.click(hit["x"], hit["y"])
                        approved = True
                        print("[muse] approved the upload/link dialog", flush=True)
                        await asyncio.sleep(3)

                # (b) only links INSIDE the chat turn we just created count —
                #     scraping every .mp4 on the page returns older clips and
                #     silently downloads the wrong file.
                found = json.loads(await t.ev("""(() => {
                    const anchors = [...document.querySelectorAll('a[href]')]
                        .filter(a => /\\.mp4/i.test(a.href));
                    if (!anchors.length) return JSON.stringify({n:0});
                    const a = anchors[anchors.length - 1];
                    const txt = (a.innerText || '').trim();
                    const box = a.closest('[class*=message],[class*=Message],[class*=turn]') || a.parentElement;
                    const ctx = box ? (box.innerText || '').slice(0, 400) : '';
                    return JSON.stringify({n: anchors.length, href: a.href, txt, ctx});
                })()"""))

                if found.get("href") and found["href"] not in before:
                    url = found["href"]
                    name = url.rsplit("/", 1)[-1]
                    print(f"[muse] clip ready: {name}", flush=True)
                    print(f"[muse] url: {url}", flush=True)
                    return url, name

                print(f"[muse] waiting... ({int(deadline-time.time())}s left)", flush=True)
            raise TimeoutError("Muse did not produce a link in time")

    url, name = asyncio.run(run())

    # download outside the page — plain curl works on the file link
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    r = subprocess.run(["curl", "-sL", "--max-time", "300", "-A", ua,
                        "-o", out_path, url], capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(out_path):
        raise RuntimeError(f"download failed: {r.stderr[:200]}")
    size = os.path.getsize(out_path)
    if size == 0:
        raise RuntimeError(f"download produced an empty file at {out_path}")

    # Verify the artifact is really a decodable video, not an HTML error page
    # saved under a .mp4 name. This is the step the README promises.
    probe_video(out_path)

    print(f"[muse] saved {out_path} ({size:,} bytes) as '{name}'", flush=True)
    return out_path


def probe_video(path):
    """Confirm `path` is a decodable video; raise with the reason otherwise.

    Returns the parsed ffprobe fields on success. A render is only trustworthy
    when a decoder agrees it is a video, so callers should treat a failure here
    as a failed render rather than a cosmetic warning.
    """
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_entries", "stream=codec_name,codec_type,width,height",
             "-show_entries", "format=duration,size",
             "-of", "json", path],
            capture_output=True, text=True, timeout=60, check=True,
        ).stdout
    except FileNotFoundError:
        raise RuntimeError(
            "ffprobe not found on PATH — install ffmpeg to verify renders")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"ffprobe rejected {path}: {e.stderr[:200]}")

    info = json.loads(out or "{}")
    streams = info.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise RuntimeError(f"no video stream in {path} — not a video file")

    fmt = info.get("format") or {}
    print(f"[verify] {os.path.basename(path)}: "
          f"codec={video.get('codec_name')} "
          f"{video.get('width')}x{video.get('height')} "
          f"duration={fmt.get('duration')}s", flush=True)
    return {"video": video, "format": fmt}


# ------------------------------------------------------------------ Gemini image

def gemini_image(prompt, out_path, timeout_s=300, poll_s=6):
    tab = find_tab("gemini.google.com")
    if not tab:
        raise RuntimeError("no gemini.google.com tab open in the debug browser")

    async def run():
        async with Tab(tab, timeout=60) as t:
            await t.cmd("Page.bringToFront")
            await t.cmd("Emulation.setFocusEmulationEnabled", {"enabled": True})
            await t.ev("1")

            txt = await t.ev("document.body.innerText")
            if "Sign in" in txt and "Sign in to save activity" in txt:
                raise RuntimeError("Gemini is NOT signed in in this browser profile "
                                   "(page shows 'Sign in'). Sign in once in this "
                                   "Thorium window, then re-run.")

            # baseline: images already on the page
            before = json.loads(await t.ev(
                "JSON.stringify([...document.querySelectorAll('img')]"
                ".map(i=>i.src).filter(s=>s.startsWith('http')||s.startsWith('blob')))"))

            composer = ("document.querySelector('rich-textarea [contenteditable=\"true\"]')"
                        " || document.querySelector('[contenteditable=\"true\"]')")
            await t.type_into(composer, prompt)
            print("[gemini] prompt typed", flush=True)
            if not await t.send_via("Send message"):
                if not await t.send_via("Send"):
                    raise RuntimeError("Gemini send button not found")
            print("[gemini] sent; polling for the generated image...", flush=True)

            deadline = time.time() + timeout_s
            while time.time() < deadline:
                await asyncio.sleep(poll_s)
                # find a NEW large image and pull it out via canvas
                data = await t.ev(f"""(() => {{
                    const before = new Set({json.dumps(before)});
                    const imgs = [...document.querySelectorAll('img')]
                        .filter(i => i.naturalWidth > 400 && !before.has(i.src));
                    if (!imgs.length) return null;
                    const im = imgs[imgs.length-1];
                    const c = document.createElement('canvas');
                    c.width = im.naturalWidth; c.height = im.naturalHeight;
                    c.getContext('2d').drawImage(im, 0, 0);
                    try {{ return c.toDataURL('image/png'); }}
                    catch(e) {{ return 'TAINTED:' + e.message; }}
                }})()""")
                if isinstance(data, str) and data.startswith("data:image"):
                    return data
                if isinstance(data, str) and data.startswith("TAINTED"):
                    raise RuntimeError(f"canvas tainted, cannot export: {data}")
                print(f"[gemini] waiting... ({int(deadline-time.time())}s left)", flush=True)
            raise TimeoutError("Gemini produced no image in time")

    data_url = asyncio.run(run())
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    open(out_path, "wb").write(base64.b64decode(data_url.split(",", 1)[1]))
    print(f"[gemini] saved {out_path} ({os.path.getsize(out_path):,} bytes)", flush=True)
    return out_path


# ----------------------------------------------------------------------- status

def status():
    try:
        tabs = list_tabs()
    except Exception as e:
        print("CDP: DOWN —", e)
        return
    print("CDP: up on", PORT)
    for t in tabs:
        if t.get("type") == "page":
            print(f"  {t.get('title','')[:45]:<45} {t.get('url','')[:60]}")
    for name, sub in (("muse.ai", "muse.ai"), ("gemini", "gemini.google.com")):
        print(f"  {name:<10} tab: {'OPEN' if find_tab(sub) else 'not open'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("kind", choices=["video", "image", "status"])
    ap.add_argument("prompt", nargs="?")
    ap.add_argument("--out")
    ap.add_argument("--timeout", type=int)
    a = ap.parse_args()

    if a.kind == "status":
        status()
        return
    if not a.prompt:
        ap.error("prompt required")

    if a.kind == "video":
        out = a.out or os.path.expanduser("~/tools/yt-video/assets/video/muse-clip.mp4")
        p = muse_video(a.prompt, out, timeout_s=a.timeout or 1800)
    else:
        out = a.out or os.path.expanduser("~/tools/yt-video/assets/images/gemini-image.png")
        p = gemini_image(a.prompt, out, timeout_s=a.timeout or 300)
    print("RESULT:", p)


if __name__ == "__main__":
    main()
