"""Build a labeled filmstrip from a rendered MP4 by sampling one frame per scene.

Usage:  python make_filmstrip.py <video.mp4> <out.png> [t1,t2,t3,...]
If no timestamps are given, samples 12 evenly-spaced frames.
"""
import subprocess, sys, os, shutil
from PIL import Image, ImageDraw

def build(video, out, stamps=None, cols=4, w=520):
    if stamps is None:
        dur = float(subprocess.check_output(
            ["ffprobe","-v","error","-show_entries","format=duration",
             "-of","default=noprint_wrappers=1:nokey=1", video]).decode().strip())
        n = 12
        stamps = [round(dur*(i+0.5)/n, 2) for i in range(n)]

    if not stamps:
        raise ValueError("no timestamps to sample — pass at least one timestamp")

    tmp = os.path.join(os.path.dirname(out) or ".", "_fs_tmp")
    os.makedirs(tmp, exist_ok=True)
    try:
        frames = []
        for i, t in enumerate(stamps):
            p = os.path.join(tmp, f"f{i:02d}.png")
            subprocess.run(["ffmpeg","-y","-v","error","-ss",str(t),"-i",video,
                            "-frames:v","1",p], check=True)
            if not os.path.exists(p):
                raise RuntimeError(f"ffmpeg produced no frame at t={t}s")
            frames.append((t, p))

        ims = [Image.open(p).convert("RGB") for _, p in frames]
        ar = ims[0].height / ims[0].width
        h = int(w * ar)
        ims = [im.resize((w, h)) for im in ims]
        rows = (len(ims) + cols - 1) // cols
        lab = 40
        canvas = Image.new("RGB", (cols*w, rows*(h+lab)), (10,10,14))
        d = ImageDraw.Draw(canvas)
        for i, (im, (t, _)) in enumerate(zip(ims, frames)):
            r, c = divmod(i, cols)
            x, y = c*w, r*(h+lab)
            canvas.paste(im, (x, y))
            d.text((x+10, y+h+10), f"t={t}s", fill=(200,210,225))
        canvas.save(out)
        print(f"wrote {out}  ({canvas.width}x{canvas.height})  frames={len(ims)}")
    finally:
        # Always remove the scratch frame directory, on success or failure —
        # otherwise every run leaves its sampled PNGs behind.
        shutil.rmtree(tmp, ignore_errors=True)

if __name__ == "__main__":
    video = sys.argv[1]
    out = sys.argv[2]
    stamps = None
    if len(sys.argv) > 3:
        stamps = [float(x) for x in sys.argv[3].split(",")]
    build(video, out, stamps)
