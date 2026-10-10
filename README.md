# Muse Browser Media

**Generate real 1080p video — and images — from a text prompt, with no API key and no paid credits.**

This toolkit drives a *logged-in browser* instead of an API. You describe a scene; a real
browser types it into Meta's **Muse** agent; Muse renders an MP4 and hands back a download
link. A small script fetches it, verifies it with `ffprobe`, and saves it to disk.

No keys. No subscriptions. No per-second billing.

---

## Why this exists

Frontier video models are expensive and gated behind APIs. Meanwhile, a logged-in browser
session already has access — it is just built for a human, not a script. This project closes
that gap: it treats the browser as the transport and the chat box as the interface.

The result is a genuinely free route to short, high-definition clips — useful for background
loops, b-roll, social cutaways, and prototyping a shot before you pay to render it properly.

## What it actually does

| Step | Action |
| --- | --- |
| 1 | Attaches to a Chromium browser over the DevTools Protocol (CDP) |
| 2 | Finds the signed-in Muse tab |
| 3 | Types your prompt into the composer and clicks **Send** |
| 4 | Waits for Muse to finish rendering |
| 5 | Approves the download dialog and captures the **new** link |
| 6 | Downloads, then verifies codec, resolution, fps and duration with `ffprobe` |

## Requirements

- **Windows** with a Chromium-family browser launched with `--remote-debugging-port=9333`
- **Python 3** with `websockets` (`python3 -m pip install websockets`)
- **ffprobe** (ships with ffmpeg) for verification
- A **signed-in Muse session** inside the browser profile you launch

> The login is yours and stays in the browser profile. This toolkit never touches, stores,
> or transmits credentials.

## Quick start

```bash
# 1. Launch the browser with the debug port and open Muse
thorium.exe --remote-debugging-port=9333 --remote-allow-origins=* \
  --user-data-dir="<your-profile-dir>" \
  --no-first-run --no-default-browser-check \
  https://muse.ai/

# 2. Sign in to Muse once, in that window.

# 3. Check that both the browser and the login are ready
python3 tools/browser_gen.py status

# 4. Generate a clip
python3 tools/browser_gen.py video \
  "Slow drifting dark navy-to-emerald gradient with faint glowing circuit lines, \
   subtle particles floating upward, calm premium tech aesthetic, no text, no logos. \
   10 seconds, 1920x1080." \
  --out assets/clip.mp4
```

### Headless mode (recommended for automation)

A visible browser holds the profile lock, so close it before running headless:

```bash
python3 tools/muse_headless.py start --min-free 0.35   # refuses below the RAM floor
python3 tools/browser_gen.py video "prompt" --out assets/clip.mp4
python3 tools/muse_headless.py stop                     # always stop — frees ~500 MB
```

## The four traps that produce a *wrong* result

These cost real debugging time. They are the reason this repository exists rather than a
one-line shell script.

1. **The download is gated by an `Allow` dialog.** Until that button is clicked, no download
   link exists on the page at all. The tool clicks it for you.
2. **Never scrape "every `.mp4` on the page".** The chat accumulates links from previous
   turns. Grabbing the last anchor silently downloads an *older* clip — and it passes a
   casual glance because it is still a valid video. The tool snapshots the anchors before
   sending and accepts only a link that is genuinely new.
3. **Links expire** (roughly 48 hours), so the file must be fetched immediately.
4. **RAM is the binding constraint.** On an 8 GB machine, free memory can sit near 0.2 GB,
   and a render swings it violently. Without a guard, a render can starve the whole system.
   `muse_headless.py` refuses to start below a floor and kills the browser if memory
   collapses mid-render.

## Verification

Never trust that a render "worked" because the script exited zero. Check the artifact:

```
$ ffprobe -v error -show_entries format=duration,size \
    -show_entries stream=codec_name,width,height,r_frame_rate,codec_type \
    -of default=noprint_wrappers=1 assets/clip.mp4

codec_name=h264
codec_type=video
width=1920
height=1080
r_frame_rate=24/1
duration=10.000000
```

A verified sample from this pipeline: **H.264, 1920×1080, 24 fps, exactly 10.0 s, 4.3 MB.**

## Honest limitations

- **Generation is not deterministic.** The same prompt can yield different results, and
  outputs occasionally contain artifacts (misaligned geometry, odd lighting). Expect to
  regenerate a shot you care about.
- **No audio guarantees.** Some renders are video-only.
- **It depends on a web UI.** If the site changes its markup, the selectors need updating.
- **It depends on a logged-in session.** Log out, and the route stops working.
- **RAM-heavy.** Do not run a render alongside other heavy work on a low-memory machine.
- **Respect the terms of service.** This automates your own account for your own use. Do not
  use it to mass-generate, resell access, or farm accounts.

## Repository layout

```
tools/
  browser_gen.py      main entry point — video | image | status
  muse_headless.py    headless browser lifecycle + RAM guard
  muse_cdp.py         minimal CDP client
  make_filmstrip.py   build a frame contact sheet from a clip
```

## Credits

Built by **Darren Lieu** — [GitHub](https://github.com/DarthSandD) ·
[Portfolio](https://darrenlin.pages.dev/) · [@darren.lin_ai](https://www.instagram.com/darren.lin_ai)

If this saved you an API bill, you can [buy me a coffee](https://saweria.co/darren111).

## Licence

MIT
