"""Generate once with a cloud backend outside Nuke, to check it against the real service.

Usage (from the repository root):

    python installers/live_check.py <backend id> [model] [--prompt "..."] [--count N] [--out DIR]
                                    [--workflows DIR]

It builds a 640x480 test crop (sky, ground, a red ball) and a mask over the ball, calls the
backend exactly like GenFill does, and writes the results plus a report to ``--out``
(default: ``test_output/live_<backend>``). OAuth backends open the browser to sign in the first
time. This spends real credits (except ComfyUI on your own machine).

ComfyUI: the model is a workflow id, e.g. ``comfyui sdxl_inpaint --workflows tests/fixtures/comfyui``.
``--workflows DIR`` adds a workflow folder (sets ``GENFILL_COMFY_WORKFLOWS``); the server comes
from ``GENFILL_COMFY_URL`` or the config (default http://127.0.0.1:8188).
"""
from __future__ import annotations

import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "nuke", "python"))

from genfill import backends, config, pngio  # noqa: E402
from genfill.backends import base  # noqa: E402

W, H = 640, 480
BALL = (320, 260, 70)


def make_inputs(folder, width, height):
    plate = pngio.Image(width, height, 3)
    mask = pngio.Image(width, height, 3)
    mask_a = pngio.Image(width, height, 4)
    cx, cy, r = (BALL[0] * width // W, BALL[1] * height // H, BALL[2] * width // W)
    for y in range(height):
        prow, mrow, arow = plate.rows[y], mask.rows[y], mask_a.rows[y]
        for x in range(width):
            if y < height * 0.6:
                c = (110 + y * 80 // height, 160 + y * 60 // height, 230)
            else:
                c = (90 + (x * 7 + y * 3) % 40, 120 + (x * 3) % 30, 60)
            d2 = (x - cx) ** 2 + (y - cy) ** 2
            if d2 < r * r:
                c = (210, 30, 30)
            prow[x * 3:x * 3 + 3] = bytes(c)
            inside = d2 < (r + 12) ** 2
            v = 255 if inside else 0
            mrow[x * 3:x * 3 + 3] = bytes((v, v, v))
            arow[x * 4:x * 4 + 4] = bytes(c + (0 if inside else 255,))
    paths = {k: os.path.join(folder, k + ".png") for k in ("source", "mask", "mask_alpha")}
    pngio.write(paths["source"], plate)
    pngio.write(paths["mask"], mask)
    pngio.write(paths["mask_alpha"], mask_a)
    return paths


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("backend")
    ap.add_argument("model", nargs="?", default="")
    ap.add_argument("--prompt", default="a small wooden crate")
    ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", default="")
    ap.add_argument("--workflows", default="",
                    help="extra ComfyUI workflow folder (sets GENFILL_COMFY_WORKFLOWS)")
    args = ap.parse_args(argv)
    if args.workflows:
        # config.comfyui_workflow_dirs() reads the variable on every call, so setting it here
        # (before the backend lists or loads a workflow) is early enough.
        os.environ["GENFILL_COMFY_WORKFLOWS"] = os.path.abspath(args.workflows)

    backend = backends.get(args.backend)
    out = args.out or os.path.join(HERE, "..", "test_output", "live_%s" % backend.id)
    if not os.path.isdir(out):
        os.makedirs(out)
    mw, mh = backend.model_size(W, H, args.model)
    paths = make_inputs(out, mw, mh)
    req = base.GenerationRequest(paths["source"], paths["mask"], paths["mask_alpha"],
                                 args.prompt, mw, mh, seed=args.seed, count=args.count,
                                 model=args.model)
    key = config.get_api_key(backend.id, backend.env_var)
    print("%s model=%r size=%dx%d prompt=%r count=%d key=%s" % (
        backend.id, args.model or backend.default_model, mw, mh, args.prompt, args.count,
        "yes" if key else "no"))

    def progress(frac, msg):
        print("  %3.0f%% %s" % (frac * 100, msg))

    t0 = time.time()
    try:
        images = backend.generate(req, key, progress=progress, cancelled=lambda: False)
    except base.BackendError as exc:
        print("FAILED after %.1fs: %s" % (time.time() - t0, exc))
        return 1
    report = []
    for i, data in enumerate(images):
        ext = base.sniff_image(data) or ".bin"
        path = os.path.join(out, "var_%02d%s" % (i + 1, ext))
        with open(path, "wb") as fh:
            fh.write(data)
        size = pngio.size_of(data) if ext == ".png" else None
        report.append("%s %d bytes %s" % (os.path.basename(path), len(data),
                                          "%dx%d" % size if size else ""))
    print("OK in %.1fs: %d image(s)" % (time.time() - t0, len(images)))
    for line in report:
        print("  " + line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
