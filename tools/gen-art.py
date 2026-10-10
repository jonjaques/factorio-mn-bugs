#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx", "numpy", "pillow", "scipy"]
# ///
"""Generate the boxelder bug and Asian beetle art with OpenAI's image models.

    uv run tools/gen-art.py status                     what is generated, missing, and what it cost
    uv run tools/gen-art.py guides   [--target T]      write the guide sheets to build/guides/ to look at
    uv run tools/gen-art.py generate [--target T] [--only run/06-SE ...] [--redo] [--dry-run]
    uv run tools/gen-art.py anchor   --target T --from run/06-SE [--frame 0]
    uv run tools/gen-art.py assemble [--target T] [--force]
    uv run tools/gen-art.py review   [--target T]      animated GIFs of art/ in build/review/

How it works. Each request repaints ONE direction of ONE animation: the vanilla
biter's frames for that direction, laid out in a grid on a flat background (the
"guide"), go to /v1/images/edits with photos of the species and, once it exists, a
frame of the target's own approved art (the "anchor"). Sending a whole direction
in one request keeps its frames consistent with each other, because they come out
of one image; per-frame requests would have nothing tying them together. The
model keeps the grid, the heading and the leg poses, so the vanilla motion
survives the repaint. The anchor ties the 48 sheets of a target together.

The model does not keep the grid pitch exactly (rows drift by ~10px each), so
`assemble` cuts the sheet up by connected component rather than by fixed window,
then moves every frame so the creature's body centroid lands on the vanilla
biter's body centroid for that frame. That puts the sprite back on its hitbox and
inherits vanilla's bob and lunge instead of the model's drift.

What is not generated, and why:
  shadow  derived from the generated colour alpha with a transform fitted to
          vanilla's own colour -> shadow pairs. Asking the model for a separate
          shadow layer cannot work; keeping vanilla's would show biter legs.
  decay   a dissolve of the last die frame. Vanilla decay is the corpse fading out
          through alpha, which image models can't produce, and starting from the
          die frame makes the hand-off seamless.
  icons   cut from a generated frame, so they match the sprite.

Raw model output is the expensive part, so it is kept in git (LFS) under
art/<target>/generated/ with a .json record of the request, and `generate` skips
any sheet that already exists unless --redo. Frames written by `assemble` stay
hand-editable: it refuses to overwrite a frame that is neither vanilla nor what it
wrote last time, unless --force.

Reads OPENAI_API_KEY from the environment, or from .env in the repo root.
"""

import argparse
import base64
import hashlib
import io
import json
import math
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import httpx
import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage, optimize

ROOT = Path(__file__).resolve().parent.parent
ART = ROOT / "art"
BUILD = ROOT / "build"
VANILLA = BUILD / "vanilla"
REFERENCE = ROOT / "reference"

API = "https://api.openai.com/v1/images/edits"
# gpt-image-2.5-sunburst is the model OpenAI describes as the most precise editor,
# and it rejects `input_fidelity`, so that parameter is never sent.
DEFAULT_MODEL = "gpt-image-2.5-sunburst"
DEFAULT_QUALITY = "high"

GUIDE_BACKGROUND = (216, 216, 216)
GUIDE_GUTTER = 48            # px between frames; enough that legs of neighbours rarely touch
ALPHA_FLOOR = 8              # vanilla alpha at or below this counts as empty when cropping

# Generated animations and their grid width. Run and attack fit 4 columns in a
# landscape sheet; die's 17 frames go 6 wide so the sheet stays under 2560px.
# Decay is not here: it is derived from die (see module docstring).
ANIMS = {
    "run":    {"cols": 4},
    "attack": {"cols": 4},
    "die":    {"cols": 6},
}

# Where the creature's head points in each direction folder, in screen terms. The
# model reads "towards the bottom-right corner" more reliably than compass names.
HEADINGS = {
    "00-N": "straight up the image, away from the viewer",
    "01-NNE": "up the image, turned slightly to the right",
    "02-NE": "towards the top-right corner",
    "03-ENE": "to the right, turned slightly upwards",
    "04-E": "straight to the right",
    "05-ESE": "to the right, turned slightly downwards",
    "06-SE": "towards the bottom-right corner",
    "07-SSE": "down the image, turned slightly to the right",
    "08-S": "straight down the image, towards the viewer",
    "09-SSW": "down the image, turned slightly to the left",
    "10-SW": "towards the bottom-left corner",
    "11-WSW": "to the left, turned slightly downwards",
    "12-W": "straight to the left",
    "13-WNW": "to the left, turned slightly upwards",
    "14-NW": "towards the top-left corner",
    "15-NNW": "up the image, turned slightly to the left",
}

# Per-target art direction. `refs` are files in reference/; `erase` paints over a
# rectangle with the photo's own background first (asian-beetle3 carries a pest
# control company's logo, which the model would otherwise happily reproduce).
CREATURES = {
    "boxelder-bug": {
        "name": "a realistic boxelder bug (Boisea trivittata)",
        "noun": "bug",
        "refs": [
            {"file": "boxelder1.png"},
            {"file": "boxelder2.png"},
            {"file": "boxelder3.jpg"},
        ],
        "anatomy": (
            "a flat, elongated body; charcoal-black forewings with thin bright red-orange edges "
            "and veins; three red-orange stripes on the pronotum; small red eyes; six thin black "
            "legs; two long, thin, segmented black antennae."
        ),
        "legs": "",
        "anims": {
            "run": "running",
            "attack": (
                "attacking. The frames show the creature lunging forward, rearing its front up "
                "to strike, and recovering. Keep that timing: the bug rears up and jabs its head "
                "and beak forward where the biter strikes, legs braced, antennae swept forward"
            ),
            "die": (
                "dying. The frames show the creature collapsing, tumbling over and coming to rest. "
                "Keep that timing: the bug staggers and tips over onto its side or back where the "
                "biter does, its legs thrash and then curl in against the body, and the last frame "
                "is a dead bug lying still"
            ),
        },
        "icon": {"anim": "run", "dir": "11-WSW", "frame": 0},
        "corpse_icon": {"anim": "die", "dir": "11-WSW", "frame": 16},
    },
    "asian-beetle": {
        "name": "a realistic Asian lady beetle (Harmonia axyridis), the orange ladybug",
        "noun": "beetle",
        "refs": [
            {"file": "asian-beetle3.jpg", "erase": [[825, 825, 985, 985]]},
            {"file": "asian-beetle.jpg"},
            {"file": "asian-beetle2.jpg"},
        ],
        "anatomy": (
            "a glossy, high-domed, almost hemispherical orange shell split by a seam down the "
            "middle, with round black spots; a cream-white pronotum with a black M-shaped mark; "
            "a small black head with cream cheek patches; six short orange-brown legs; short "
            "clubbed antennae."
        ),
        "legs": (
            " A lady beetle's legs are far shorter than the biter's: keep the body's position, "
            "heading and rhythm, but draw short beetle legs that just show beyond the shell "
            "instead of copying the biter's long legs."
        ),
        "anims": {
            "run": "running",
            "attack": (
                "attacking. The frames show the creature lunging forward, rearing its front up "
                "to strike, and recovering. Keep that timing: the beetle lunges and the front of "
                "its shell tips up where the biter strikes, snapping with its mandibles"
            ),
            "die": (
                "dying. The frames show the creature collapsing, tumbling over and coming to rest. "
                "Keep that timing: the beetle topples over onto its back where the biter falls, its "
                "short legs flail and then fold in, and the last frame is a dead beetle lying still"
            ),
        },
        "icon": {"anim": "run", "dir": "11-WSW", "frame": 0},
        "corpse_icon": {"anim": "die", "dir": "11-WSW", "frame": 16},
    },
}

PROMPT = """\
Image 1 is a sprite sheet from the video game Factorio: {n} animation frames of a "biter" creature {anim}, laid out in a {cols}x{rows} grid read left to right, top to bottom.{empty} Every frame faces the same direction, {heading}, and is seen from the game's fixed high three-quarter top-down camera.

Redraw this sprite sheet with the creature replaced by {name}. {refs_line} {anatomy}{anchor_line}

For every frame keep exactly: the grid layout, the frame's position and size within its cell, the heading direction, the camera angle, and the pose and leg positions, so the frames still play back as the same animation.{legs} The {noun} should cover about the same area as the biter's body and legs.

Style: Factorio's pre-rendered 3D sprite art: realistic, slightly desaturated, lit softly from the top-left, with detail that stays readable when shrunk. Every frame shows the identical {noun}: same colours, markings and proportions. Fully transparent background. No shadow on the ground, no grid lines, no text, no labels, no extra creatures."""


# --------------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------------

def ensure_vanilla() -> Path:
    """Vanilla frames are the guides and the alignment targets, so they come from a
    fresh extraction rather than art/, which this tool overwrites."""
    if not (VANILLA / "boxelder-bug" / "manifest.json").exists():
        print("extracting vanilla biter frames to build/vanilla/ ...", file=sys.stderr)
        shutil.rmtree(VANILLA, ignore_errors=True)
        subprocess.run(["uv", "run", str(ROOT / "tools/extract-biter-art.py"), "--out", str(VANILLA)], check=True)
    return VANILLA


def manifest(target: str, base: Path = ART) -> dict:
    return json.loads((base / target / "manifest.json").read_text())


def vanilla_frames(target: str, anim: str, direction: str, layer: str = "color") -> list[Image.Image]:
    m = manifest(target, VANILLA)
    n = m["animations"][anim]["frames_per_direction"]
    src = VANILLA / target / anim / layer / direction
    return [Image.open(src / f"{f:02d}.png").convert("RGBA") for f in range(n)]


def guide(target: str, anim: str, direction: str) -> tuple[Image.Image, dict]:
    """One direction's vanilla frames in a grid. Every frame is cut with the same
    crop (the union of their content), so motion within the cell is preserved."""
    frames = vanilla_frames(target, anim, direction)
    box = None
    for im in frames:
        b = im.getchannel("A").point(lambda v: 255 if v > ALPHA_FLOOR else 0).getbbox()
        box = b if box is None else (min(box[0], b[0]), min(box[1], b[1]), max(box[2], b[2]), max(box[3], b[3]))
    g = GUIDE_GUTTER
    cw, ch = box[2] - box[0] + g, box[3] - box[1] + g
    cols = ANIMS[anim]["cols"]
    rows = math.ceil(len(frames) / cols)
    # Custom sizes must be multiples of 16 on both edges.
    w, h = math.ceil(cols * cw / 16) * 16, math.ceil(rows * ch / 16) * 16
    sheet = Image.new("RGBA", (w, h), GUIDE_BACKGROUND + (255,))
    ox, oy = (w - cols * cw) // 2, (h - rows * ch) // 2
    cells = []
    for i, im in enumerate(frames):
        r, c = divmod(i, cols)
        x, y = ox + c * cw + g // 2, oy + r * ch + g // 2
        sheet.alpha_composite(im.crop(box), (x, y))
        cells.append([x, y])
    layout = {"crop": list(box), "cell": [cw, ch], "cells": cells, "size": [w, h], "cols": cols, "rows": rows}
    return sheet.convert("RGB"), layout


def ref_image(spec: dict) -> Image.Image:
    # via RGBA: some references are palette PNGs with transparency, which PIL warns about otherwise
    im = Image.open(REFERENCE / spec["file"]).convert("RGBA").convert("RGB")
    for x0, y0, x1, y1 in spec.get("erase", []):
        # Fill with the median colour of a ring just outside the box: crude, but the
        # photo is only there for colouring and anatomy.
        arr = np.asarray(im).copy()
        ring = np.concatenate([arr[max(0, y0 - 12):y0, x0:x1].reshape(-1, 3), arr[y1:y1 + 12, x0:x1].reshape(-1, 3),
                               arr[y0:y1, max(0, x0 - 12):x0].reshape(-1, 3), arr[y0:y1, x1:x1 + 12].reshape(-1, 3)])
        arr[y0:y1, x0:x1] = np.median(ring, axis=0).astype(np.uint8)
        im = Image.fromarray(arr)
    im.thumbnail((1024, 1024), Image.LANCZOS)
    return im


def data_url(im: Image.Image, fmt: str = "PNG") -> str:
    buf = io.BytesIO()
    im.save(buf, fmt, **({"quality": 92} if fmt == "JPEG" else {}))
    return f"data:image/{fmt.lower()};base64," + base64.b64encode(buf.getvalue()).decode()


def anchor_path(target: str) -> Path:
    return ART / target / "generated" / "anchor.png"


def prompt_for(target: str, anim: str, direction: str, layout: dict, n: int, has_anchor: bool) -> str:
    c = CREATURES[target]
    k = len(c["refs"])
    empty = layout["cols"] * layout["rows"] - n
    refs_line = (f"Images 2 to {k + 1} are photographs of the species, for anatomy and colouring only:"
                 if k > 1 else "Image 2 is a photograph of the species, for anatomy and colouring only:")
    anchor_line = (f"\n\nImage {k + 2} is this exact {c['noun']} as already drawn for the game. Match its colours, "
                   f"markings, proportions and rendering exactly, but take the pose and direction from image 1."
                   if has_anchor else "")
    return PROMPT.format(
        n=n, anim=c["anims"][anim], cols=layout["cols"], rows=layout["rows"],
        empty=(f" The last {'cell is' if empty == 1 else f'{empty} cells are'} empty; leave {'it' if empty == 1 else 'them'} empty." if empty else ""),
        heading=HEADINGS[direction], name=c["name"], refs_line=refs_line, anatomy=c["anatomy"],
        anchor_line=anchor_line, legs=c["legs"], noun=c["noun"],
    )


# --------------------------------------------------------------------------------
# generate
# --------------------------------------------------------------------------------

def api_key() -> str:
    if os.environ.get("OPENAI_API_KEY"):
        return os.environ["OPENAI_API_KEY"]
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            k, _, v = line.partition("=")
            if k.strip() == "OPENAI_API_KEY":
                return v.strip().strip('"').strip("'")
    raise SystemExit("OPENAI_API_KEY is not set and not in .env")


def sheet_path(target: str, anim: str, direction: str) -> Path:
    return ART / target / "generated" / anim / f"{direction}.png"


def all_jobs(targets: list[str]) -> list[tuple[str, str, str]]:
    m = manifest(targets[0], VANILLA)
    return [(t, a, d) for t in targets for a in ANIMS for d in m["directions"]]


def select(args) -> list[tuple[str, str, str]]:
    targets = [args.target] if args.target else list(CREATURES)
    jobs = all_jobs(targets)
    if args.only:
        wanted = set(args.only)
        jobs = [j for j in jobs if f"{j[1]}/{j[2]}" in wanted or j[1] in wanted]
    return jobs


def request_one(key: str, target: str, anim: str, direction: str, model: str, quality: str, dry: bool) -> str:
    sheet, layout = guide(target, anim, direction)
    n = len(vanilla_frames(target, anim, direction))
    anchor = anchor_path(target)
    images = [data_url(sheet)] + [data_url(ref_image(r), "JPEG") for r in CREATURES[target]["refs"]]
    if anchor.exists():
        images.append(data_url(Image.open(anchor).convert("RGB")))
    prompt = prompt_for(target, anim, direction, layout, n, anchor.exists())
    w, h = layout["size"]
    body = {
        "model": model,
        "prompt": prompt,
        "images": [{"image_url": u} for u in images],
        "size": f"{w}x{h}",
        "quality": quality,
        # Transparent output is real alpha (the RGB under it is junk), which saves
        # keying a background colour out of a red and orange subject.
        "background": "transparent",
        "output_format": "png",
    }
    label = f"{target} {anim}/{direction}"
    if dry:
        return f"{label}: {w}x{h}, {len(images)} images, prompt {len(prompt)} chars"

    for attempt in range(6):
        t0 = time.time()
        try:
            r = httpx.post(API, json=body, headers={"Authorization": f"Bearer {key}"}, timeout=900)
        except httpx.TransportError as e:
            wait = 10 * 2 ** attempt
            print(f"{label}: {e!r}; retrying in {wait}s", file=sys.stderr)
            time.sleep(wait)
            continue
        if r.status_code == 200:
            break
        if r.status_code in (429, 500, 502, 503, 504):
            wait = float(r.headers.get("retry-after", 10 * 2 ** attempt))
            print(f"{label}: HTTP {r.status_code}; retrying in {wait:.0f}s", file=sys.stderr)
            time.sleep(wait)
            continue
        raise RuntimeError(f"{label}: HTTP {r.status_code}: {r.text[:500]}")
    else:
        raise RuntimeError(f"{label}: gave up after retries")

    j = r.json()
    out = sheet_path(target, anim, direction)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(base64.b64decode(j["data"][0]["b64_json"]))
    record = {
        "model": model,
        "quality": quality,
        "size": f"{w}x{h}",
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "seconds": round(time.time() - t0, 1),
        "usage": j.get("usage"),
        "anchor_sha1": hashlib.sha1(anchor.read_bytes()).hexdigest() if anchor.exists() else None,
        "refs": [r["file"] for r in CREATURES[target]["refs"]],
        "layout": layout,
        "prompt": prompt,
    }
    out.with_suffix(".json").write_text(json.dumps(record, indent=2) + "\n")
    return f"{label}: {record['seconds']}s, {cost(record):.3f} USD"


# Per 1M tokens (input, output), from the model pages at developers.openai.com.
# Only used to report spend; check the dashboard for the real bill.
PRICES = {"gpt-image-2.5-sunburst": (5.0, 30.0)}


def cost(record: dict) -> float:
    u = record.get("usage") or {}
    pin, pout = PRICES.get(record.get("model"), (0.0, 0.0))
    return (u.get("input_tokens", 0) * pin + u.get("output_tokens", 0) * pout) / 1e6


def cmd_generate(args):
    ensure_vanilla()
    jobs = select(args)
    todo = [j for j in jobs if args.redo or not sheet_path(*j).exists()]
    print(f"{len(todo)} of {len(jobs)} selected sheets to generate ({len(jobs) - len(todo)} already exist)")
    if not todo:
        return
    key = "" if args.dry_run else api_key()
    failures = []
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futs = {pool.submit(request_one, key, *j, args.model, args.quality, args.dry_run): j for j in todo}
        for fut in as_completed(futs):
            try:
                print(fut.result(), flush=True)
            except Exception as e:  # keep the rest of the run going; report at the end
                failures.append(f"{e}")
                print(f"FAILED {e}", file=sys.stderr, flush=True)
    if failures:
        raise SystemExit(f"{len(failures)} request(s) failed; re-run the same command to retry just those")


def cmd_status(args):
    ensure_vanilla()
    targets = [args.target] if args.target else list(CREATURES)
    total = 0.0
    for t in targets:
        have = missing = 0
        spend = 0.0
        stale = []
        anchor = anchor_path(t)
        anchor_sha = hashlib.sha1(anchor.read_bytes()).hexdigest() if anchor.exists() else None
        anchor_src = json.loads(anchor.with_suffix(".json").read_text())["from"] if anchor.exists() else None
        for _, a, d in all_jobs([t]):
            p = sheet_path(t, a, d)
            if p.exists():
                have += 1
                rec = json.loads(p.with_suffix(".json").read_text())
                spend += cost(rec)
                # The anchor's own source sheet necessarily predates the anchor.
                if rec.get("anchor_sha1") != anchor_sha and f"{a}/{d}" != anchor_src:
                    stale.append(f"{a}/{d}")
            else:
                missing += 1
        total += spend
        print(f"{t}: {have} generated, {missing} missing, {spend:.2f} USD; anchor {'present' if anchor_sha else 'missing'}")
        if stale:
            print(f"  made with a different anchor: {' '.join(stale)}")
    print(f"total {total:.2f} USD")


def cmd_guides(args):
    ensure_vanilla()
    for t, a, d in select(args):
        sheet, layout = guide(t, a, d)
        out = BUILD / "guides" / t / a / f"{d}.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        sheet.save(out)
    print(f"wrote {BUILD / 'guides'}")


# --------------------------------------------------------------------------------
# Slicing and cleanup
# --------------------------------------------------------------------------------

def levels(alpha: np.ndarray) -> np.ndarray:
    # The model leaves a faint haze (alpha 1-30) around the subject and specks over
    # the background, and never quite reaches 255 inside it.
    return np.clip((alpha.astype(np.float32) - 24) / (250 - 24), 0, 1)


# Frames are sliced onto a canvas this much bigger on every side, so a frame the
# model drew off-position survives until alignment moves it back.
SLICE_PAD = 96


def slice_sheet(target: str, anim: str, direction: str) -> tuple[list[np.ndarray], list[str]]:
    """Cut a generated sheet into frame canvases (H+2P x W+2P x 4 float, alpha 0-1,
    P = SLICE_PAD), not yet aligned."""
    path = sheet_path(target, anim, direction)
    rec = json.loads(path.with_suffix(".json").read_text())
    _, layout = guide(target, anim, direction)
    if rec["layout"] != layout:
        raise SystemExit(f"{path.relative_to(ROOT)} was generated from a different guide layout; regenerate it")
    spec = manifest(target, VANILLA)["animations"][anim]
    n = spec["frames_per_direction"]
    W, H = spec["color"]["width"], spec["color"]["height"]
    L, T, R, B = layout["crop"]

    px = np.asarray(Image.open(path).convert("RGBA")).astype(np.float32)
    if px.shape[:2] != (layout["size"][1], layout["size"][0]):
        raise SystemExit(f"{path.relative_to(ROOT)} is {px.shape[1]}x{px.shape[0]}, expected {layout['size']}")
    alpha = levels(px[..., 3])
    rgb = px[..., :3] / 255

    # Assign each connected blob to the nearest cell centre instead of cutting fixed
    # windows: the model's rows drift by up to a third of the gutter, and a fixed
    # window would clip a leg off whichever frame drifted furthest.
    centres = np.array([[x + (R - L) / 2, y + (B - T) / 2] for x, y in layout["cells"]])
    solid = alpha > 0.15
    labels, count = ndimage.label(solid, structure=np.ones((3, 3)))
    owner = np.full(count + 1, -1)
    sizes = ndimage.sum(solid, labels, range(1, count + 1))
    coms = ndimage.center_of_mass(solid, labels, range(1, count + 1))
    slices = ndimage.find_objects(labels)
    split = []
    for i in range(count):
        if sizes[i] < 40:
            continue
        sl = slices[i]
        if (sl[1].stop - sl[1].start) > 1.3 * (R - L + GUIDE_GUTTER) or (sl[0].stop - sl[0].start) > 1.3 * (B - T + GUIDE_GUTTER):
            split.append(i + 1)   # spans neighbours: legs touching. Split by nearest centre below.
            continue
        cy, cx = coms[i]
        owner[i + 1] = int(np.argmin(((centres - [cx, cy]) ** 2).sum(1)))

    yy, xx = np.mgrid[0:alpha.shape[0], 0:alpha.shape[1]]
    nearest = None
    if split:
        d = np.stack([(xx - cx) ** 2 + (yy - cy) ** 2 for cx, cy in centres])
        nearest = d.argmin(0)

    warnings = []
    frames = []
    areas = []
    for k in range(n):
        mask = np.isin(labels, np.flatnonzero(owner == k))
        if split:
            mask |= np.isin(labels, split) & (nearest == k)
        ys, xs = np.nonzero(mask)
        if ys.size:
            # Work in a window around this frame; the whole sheet is ~2M pixels.
            y0, y1 = max(ys.min() - 40, 0), min(ys.max() + 41, mask.shape[0])
            x0, x1 = max(xs.min() - 40, 0), min(xs.max() + 41, mask.shape[1])
            sub = mask[y0:y1, x0:x1]
            lab, m = ndimage.label(sub, structure=np.ones((3, 3)))
            s = ndimage.sum(sub, lab, range(1, m + 1))
            # Keep the body, plus pieces near it that aren't crumbs: a leg or antenna
            # segment the alpha threshold cut loose is close by, a stray speck is not.
            near = ndimage.binary_dilation(lab == 1 + int(np.argmax(s)), iterations=24)
            hits = ndimage.maximum(near, lab, range(1, m + 1))
            keep = 1 + np.flatnonzero((s >= max(60, s.max() * 0.004)) & (hits > 0))
            sub = ndimage.binary_dilation(np.isin(lab, keep), iterations=2)
            mask = np.zeros_like(mask)
            mask[y0:y1, x0:x1] = sub
        a = alpha * mask
        areas.append(float((a > 0.5).sum()))
        # Sheet pixel (x, y) is canvas pixel (x - cell_x + L, y - cell_y + T), plus the pad.
        P = SLICE_PAD
        cx0, cy0 = layout["cells"][k][0] - L - P, layout["cells"][k][1] - T - P
        canvas = np.zeros((H + 2 * P, W + 2 * P, 4), np.float32)
        sx0, sy0 = max(cx0, 0), max(cy0, 0)
        sx1, sy1 = min(cx0 + W + 2 * P, a.shape[1]), min(cy0 + H + 2 * P, a.shape[0])
        canvas[sy0 - cy0:sy1 - cy0, sx0 - cx0:sx1 - cx0, :3] = rgb[sy0:sy1, sx0:sx1]
        canvas[sy0 - cy0:sy1 - cy0, sx0 - cx0:sx1 - cx0, 3] = a[sy0:sy1, sx0:sx1]
        frames.append(canvas)

    med = float(np.median(areas))
    for k, ar in enumerate(areas):
        if ar < 0.5 * med or ar > 1.8 * med:
            warnings.append(f"frame {k}: area {ar:.0f}px vs median {med:.0f}px (missing or merged creature?)")
    return frames, warnings


def body_centroid(alpha: np.ndarray, r: int = 9) -> np.ndarray:
    """Centroid of the body alone. An opening strips legs and antennae, which move
    independently of the body and would drag the centroid around between frames."""
    solid = alpha > 0.5
    yy, xx = np.mgrid[-r:r + 1, -r:r + 1]
    body = ndimage.binary_opening(solid, structure=(xx ** 2 + yy ** 2) <= r * r)
    if body.sum() < 50:
        body = solid
    ys, xs = np.nonzero(body)
    return np.array([xs.mean(), ys.mean()])


def translate(px: np.ndarray, dx: float, dy: float) -> np.ndarray:
    # Premultiply so the bilinear resample doesn't pull junk RGB in from transparent pixels.
    pre = px.copy()
    pre[..., :3] *= pre[..., 3:]
    out = np.stack([ndimage.shift(pre[..., c], (dy, dx), order=1, mode="constant") for c in range(4)], -1)
    a = out[..., 3:]
    out[..., :3] = np.where(a > 1e-4, out[..., :3] / np.maximum(a, 1e-4), 0)
    return out


def aligned_frames(target: str, anim: str, direction: str) -> tuple[list[np.ndarray], list[str]]:
    frames, warnings = slice_sheet(target, anim, direction)
    van = vanilla_frames(target, anim, direction)
    P = SLICE_PAD
    out = []
    for k, (g, v) in enumerate(zip(frames, van)):
        va = np.asarray(v)[..., 3].astype(np.float32) / 255
        if (g[..., 3] > 0.5).sum() >= 50:
            d = body_centroid(va) + P - body_centroid(g[..., 3])
            g = translate(g, d[0], d[1])
        frame = g[P:P + va.shape[0], P:P + va.shape[1]]
        lost = g[..., 3].sum() - frame[..., 3].sum()
        if lost > 0.02 * max(g[..., 3].sum(), 1):
            warnings.append(f"frame {k}: {lost / g[..., 3].sum():.0%} of it falls outside the canvas")
        out.append(frame)
    return out, warnings


def to_image(px: np.ndarray) -> Image.Image:
    return Image.fromarray(np.clip(np.rint(px * 255), 0, 255).astype(np.uint8), "RGBA")


# --------------------------------------------------------------------------------
# Shadows
# --------------------------------------------------------------------------------

def entity_origin(meta: dict) -> tuple[float, float]:
    """Top-left of a canvas in entity coordinates, in sheet pixels (by_pixel x 2)."""
    return meta["shift_by_pixel"][0] * 2 - meta["width"] / 2, meta["shift_by_pixel"][1] * 2 - meta["height"] / 2


def shadow_sampler(params, cmeta, smeta):
    """Matrix and offset for ndimage.affine_transform, mapping shadow-canvas (row, col)
    to colour-canvas (row, col).

    A sprite pixel's height above the ground shows up as screen-space height, and
    the sun throws it right and down by an amount proportional to that height. With
    height taken as distance above a reference line, the shadow is a shear plus a
    vertical scale and an offset of the colour silhouette: shadow = colour sheared
    by k, squashed by m, moved by (dx, dy)."""
    k, m, dx, dy = params
    cox, coy = entity_origin(cmeta)
    sox, soy = entity_origin(smeta)
    # shadow entity (ex, ey) <- colour entity (cx, cy):
    #   ex = cx + k * (-cy) + dx ;  ey = m * cy + dy
    # so cy = (ey - dy) / m ;  cx = ex - dx + k * cy
    # rows/cols: ey = r_s + soy, ex = c_s + sox ; r_c = cy - coy, c_c = cx - cox
    a = 1 / m
    matrix = np.array([[a, 0], [k * a, 1]])
    offset = np.array([(soy - dy) * a - coy, (sox - dx) + k * (soy - dy) * a - cox])
    return matrix, offset


def project_shadow(alpha: np.ndarray, params, cmeta, smeta) -> np.ndarray:
    matrix, offset = shadow_sampler(params, cmeta, smeta)
    return ndimage.affine_transform(alpha, matrix, offset, output_shape=(smeta["height"], smeta["width"]), order=1)


def fit_shadow(target: str, anim: str) -> tuple[list[float], float]:
    """Fit (k, m, dx, dy) so the vanilla colour silhouette, projected, best covers the
    vanilla shadow. Fitted on a sample of frames from every direction.

    Both targets draw the same vanilla sheets (a tier only changes tint and scale),
    so the fit depends on the animation alone and is cached in build/."""
    cache_path = BUILD / "shadow-fit.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    if anim in cache:
        return cache[anim]["params"], cache[anim]["iou"]
    m = manifest(target, VANILLA)
    spec = m["animations"][anim]
    cmeta, smeta = spec["color"], spec["shadow"]
    pairs = []
    for d in m["directions"]:
        col = vanilla_frames(target, anim, d)
        sh = vanilla_frames(target, anim, d, "shadow")
        for f in (0, len(col) // 2):
            pairs.append((np.asarray(col[f])[..., 3] / 255.0, np.asarray(sh[f])[..., 3] / 255.0))

    def loss(p):
        if p[1] < 0.2:
            return 1.0
        inter = union = 0.0
        for c, s in pairs:
            proj = project_shadow(c, p, cmeta, smeta)
            inter += np.minimum(proj, s).sum()
            union += np.maximum(proj, s).sum()
        return 1 - inter / union

    best = None
    # Coarse grid first: Nelder-Mead alone settles in whichever local minimum is nearest.
    for k in (0.0, 0.5, 1.0):
        for mm in (0.6, 1.0):
            for dx in (0, 30, 60):
                p0 = [k, mm, dx, 0]
                l0 = loss(p0)
                if best is None or l0 < best[1]:
                    best = (p0, l0)
    # Explicit simplex: the default step for a parameter starting at 0 is 0.00025,
    # far too small to move a pixel offset anywhere.
    steps = np.diag([0.2, 0.1, 10.0, 10.0])
    simplex = np.vstack([best[0], best[0] + steps])
    res = optimize.minimize(loss, best[0], method="Nelder-Mead",
                            options={"xatol": 0.05, "fatol": 1e-4, "maxiter": 400, "initial_simplex": simplex})
    params, iou = [float(v) for v in res.x], float(1 - res.fun)
    cache[anim] = {"params": params, "iou": iou}
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, indent=2) + "\n")
    return params, iou


def shadow_from(alpha: np.ndarray, params, cmeta, smeta) -> np.ndarray:
    proj = project_shadow(alpha, params, cmeta, smeta)
    # Vanilla shadows are solid black with a soft edge a couple of pixels wide.
    proj = ndimage.gaussian_filter(np.clip(proj * 1.2, 0, 1), 1.2)
    out = np.zeros(proj.shape + (4,), np.float32)
    out[..., 3] = proj
    return out


# --------------------------------------------------------------------------------
# Decay
# --------------------------------------------------------------------------------

def decay_frames(corpse: np.ndarray, n: int, seed: int) -> list[np.ndarray]:
    """Dissolve the corpse over n frames: thin parts (legs, antennae) and noisy
    patches go first, colour dries towards brown, and the tail fades out."""
    a = corpse[..., 3]
    if a.max() == 0:
        return [corpse.copy() for _ in range(n)]
    rng = np.random.default_rng(seed)
    noise = ndimage.gaussian_filter(rng.random(a.shape), 2.5)
    thick = ndimage.distance_transform_edt(a > 0.5)
    thick = np.clip(thick / max(np.percentile(thick[a > 0.5], 95), 1), 0, 1)
    # Mostly noise, so the corpse breaks into fragments as vanilla's does; thickness
    # weighted alone just shrinks the outline inwards.
    score = 0.7 * noise + 0.3 * thick
    inside = a > 0
    # Rank-normalise so the dissolve sweeps evenly through the corpse whatever its shape.
    ranks = np.zeros_like(score)
    order = score[inside].argsort().argsort()
    ranks[inside] = order / max(order.size - 1, 1)
    grey = corpse[..., :3].mean(-1, keepdims=True)
    dried = grey * 0.55 + np.array([0.20, 0.13, 0.08]) * 0.45
    frames = []
    for f in range(n):
        t = f / (n - 1)
        thr = t * 1.1 - 0.06
        keep = np.clip((ranks - thr) / 0.06, 0, 1)
        fade = 1 - 0.9 * np.clip((t - 0.65) / 0.35, 0, 1)
        px = corpse.copy()
        dry = min(1.0, 1.6 * t)
        px[..., :3] = corpse[..., :3] * (1 - 0.8 * dry) + dried * (0.8 * dry)
        px[..., 3] = a * keep * fade
        frames.append(px)
    return frames


def place_into(px: np.ndarray, src_meta: dict, dst_meta: dict) -> np.ndarray:
    """Move a frame from one canvas to another, keeping its entity-relative position."""
    sx, sy = entity_origin(src_meta)
    dx, dy = entity_origin(dst_meta)
    ox, oy = int(round(sx - dx)), int(round(sy - dy))
    out = np.zeros((dst_meta["height"], dst_meta["width"], 4), np.float32)
    h, w = px.shape[:2]
    x0, y0 = max(ox, 0), max(oy, 0)
    x1, y1 = min(ox + w, out.shape[1]), min(oy + h, out.shape[0])
    out[y0:y1, x0:x1] = px[y0 - oy:y1 - oy, x0 - ox:x1 - ox]
    return out


# --------------------------------------------------------------------------------
# Icons and previews
# --------------------------------------------------------------------------------

def make_icon(px: np.ndarray) -> Image.Image:
    """64px icon with 32/16/8 mipmaps laid out to the right, as vanilla's are (120x64)."""
    im = to_image(px)
    im = im.crop(im.getchannel("A").point(lambda v: 255 if v > 16 else 0).getbbox())
    side = max(im.size)
    square = Image.new("RGBA", (side, side))
    square.alpha_composite(im, ((side - im.width) // 2, (side - im.height) // 2))
    out = Image.new("RGBA", (120, 64))
    x = 0
    for size in (64, 32, 16, 8):
        inner = max(1, round(size * 0.94))
        level = Image.new("RGBA", (size, size))
        level.alpha_composite(square.resize((inner, inner), Image.LANCZOS), ((size - inner) // 2, (size - inner) // 2))
        out.alpha_composite(level, (x, 0))
        x += size
    return out


PREVIEW_SCALE = 0.25
PREVIEW_LABEL_WIDTH = 56
PREVIEW_BACKGROUND = (111, 125, 92, 255)   # matches extract-biter-art.py
PREVIEW_SHADOW_ALPHA = 0.5


def preview(directions, color: dict, shadow: dict | None, cmeta: dict, smeta: dict | None) -> Image.Image:
    """Contact sheet in the same format extract-biter-art.py writes: one row per direction."""
    rects = [entity_origin(cmeta) + (cmeta["width"], cmeta["height"])]
    if smeta:
        rects.append(entity_origin(smeta) + (smeta["width"], smeta["height"]))
    left = min(r[0] for r in rects); top = min(r[1] for r in rects)
    right = max(r[0] + r[2] for r in rects); bottom = max(r[1] + r[3] for r in rects)
    pw, ph = int(round(right - left)), int(round(bottom - top))
    cell = (round(pw * PREVIEW_SCALE), round(ph * PREVIEW_SCALE))
    n = len(color[directions[0]])
    sheet = Image.new("RGBA", (PREVIEW_LABEL_WIDTH + cell[0] * n, cell[1] * len(directions)), PREVIEW_BACKGROUND)
    draw = ImageDraw.Draw(sheet)
    pmeta = {"width": pw, "height": ph, "shift_by_pixel": [(left + pw / 2) / 2, (top + ph / 2) / 2]}
    for row, d in enumerate(directions):
        draw.text((8, row * cell[1] + cell[1] // 2 - 6), d, fill=(255, 255, 255, 255))
        for f in range(n):
            frame = Image.new("RGBA", (pw, ph))
            if shadow:
                s = place_into(shadow[d][f], smeta, pmeta)
                s[..., 3] *= PREVIEW_SHADOW_ALPHA
                frame.alpha_composite(to_image(s))
            frame.alpha_composite(to_image(place_into(color[d][f], cmeta, pmeta)))
            sheet.alpha_composite(frame.resize(cell, Image.LANCZOS), (PREVIEW_LABEL_WIDTH + f * cell[0], row * cell[1]))
    return sheet


# --------------------------------------------------------------------------------
# anchor / assemble
# --------------------------------------------------------------------------------

def colour_drift(colors: dict) -> dict[str, list[int]]:
    """Frames whose colours stray from their animation's norm, by sheet.

    The model sometimes loses the species partway through a sheet and drifts back
    towards the biter it is painting over (seen once: attack/02-NE, frames 6-10 a
    brown segmented abdomen). Each frame's alpha-weighted colour histogram is
    compared with the target's median run frame (Hellinger distance); a frame is
    flagged when that distance is an outlier for its own animation, because die
    frames legitimately look different (a bug on its back shows its underside)."""
    def hist(px):
        sel = px[..., 3] > 0.5
        h, _ = np.histogramdd(px[..., :3][sel], bins=(6, 6, 6), range=((0, 1),) * 3)
        return h.ravel() / max(h.sum(), 1)

    hists = {(a, d, f): hist(px) for a in ANIMS for d, frames in colors[a].items() for f, px in enumerate(frames)}
    ref = np.median(np.stack([h for k, h in hists.items() if k[0] == "run"]), 0)
    ref /= ref.sum()
    dist = {k: float(np.sqrt(max(0.0, 1 - np.sum(np.sqrt(h * ref))))) for k, h in hists.items()}
    flagged: dict[str, list[int]] = {}
    for a in ANIMS:
        v = np.array([x for k, x in dist.items() if k[0] == a])
        med = np.median(v)
        mad = 1.4826 * np.median(np.abs(v - med))
        # 4 MADs flagged exactly the drifted frames above and nothing else in 1,000+
        # boxelder frames; 3 starts catching ordinary pose changes.
        for (aa, d, f), x in dist.items():
            if aa == a and x > med + 4 * mad:
                flagged.setdefault(f"{a}/{d}", []).append(f)
    return flagged


def cmd_anchor(args):
    ensure_vanilla()
    anim, direction = args.source.split("/")
    frames, warnings = slice_sheet(args.target, anim, direction)
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    im = to_image(frames[args.frame])
    im = im.crop(im.getchannel("A").point(lambda v: 255 if v > 16 else 0).getbbox())
    pad = 24
    bg = Image.new("RGBA", (im.width + 2 * pad, im.height + 2 * pad), GUIDE_BACKGROUND + (255,))
    bg.alpha_composite(im, (pad, pad))
    # Upscaled so the model sees the markings, not a 250px thumbnail.
    scale = 768 / max(bg.size)
    bg = bg.resize((round(bg.width * scale), round(bg.height * scale)), Image.LANCZOS)
    out = anchor_path(args.target)
    out.parent.mkdir(parents=True, exist_ok=True)
    bg.convert("RGB").save(out)
    out.with_suffix(".json").write_text(json.dumps({"from": args.source, "frame": args.frame}) + "\n")
    print(f"wrote {out.relative_to(ROOT)} from {args.source} frame {args.frame}")


def sha1(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


def assemble_target(target: str, force: bool):
    vm = manifest(target, VANILLA)
    directions = vm["directions"]
    missing = [f"{a}/{d}" for _, a, d in all_jobs([target]) if not sheet_path(target, a, d).exists()]
    if missing:
        raise SystemExit(f"{target}: {len(missing)} sheet(s) not generated yet, e.g. {', '.join(missing[:4])}")

    out_root = ART / target
    index_path = out_root / "generated" / "assembled.json"
    index = json.loads(index_path.read_text()) if index_path.exists() else {}

    # Build everything in memory first, so a failure writes nothing.
    files: dict[str, Image.Image] = {}
    report = []
    colors: dict[str, dict] = {}
    shadows: dict[str, dict] = {}
    fits = {}
    for anim in ANIMS:
        spec = vm["animations"][anim]
        colors[anim], shadows[anim] = {}, {}
        if "shadow" in spec:
            fits[anim] = fit_shadow(target, anim)
            report.append(f"{anim}: shadow fit IoU {fits[anim][1]:.2f} on vanilla, params {[round(v, 2) for v in fits[anim][0]]}")
        for d in directions:
            frames, warnings = aligned_frames(target, anim, d)
            report += [f"{anim}/{d} {w}" for w in warnings]
            colors[anim][d] = frames
            if anim in fits:
                shadows[anim][d] = [shadow_from(f[..., 3], fits[anim][0], spec["color"], spec["shadow"]) for f in frames]

    dspec = vm["animations"]["decay"]
    colors["decay"] = {}
    for i, d in enumerate(directions):
        corpse = place_into(colors["die"][d][-1], vm["animations"]["die"]["color"], dspec["color"])
        colors["decay"][d] = decay_frames(corpse, dspec["frames_per_direction"], seed=i)

    for anim, by_dir in colors.items():
        for d, frames in by_dir.items():
            for f, px in enumerate(frames):
                files[f"{anim}/color/{d}/{f:02d}.png"] = to_image(px)
    for anim, by_dir in shadows.items():
        for d, frames in by_dir.items():
            for f, px in enumerate(frames):
                files[f"{anim}/shadow/{d}/{f:02d}.png"] = to_image(px)

    c = CREATURES[target]
    for role, suffix in (("icon", ""), ("corpse_icon", "-corpse")):
        src = c[role]
        files[f"icons/{target}{suffix}.png"] = make_icon(colors[src["anim"]][src["dir"]][src["frame"]])

    # Refuse to overwrite hand edits: a file is fair game only if it is still
    # vanilla or still exactly what the last assemble wrote.
    edited = []
    for rel in files:
        dst = out_root / rel
        if dst.exists() and not force:
            h = sha1(dst)
            van = VANILLA / target / rel
            if h != index.get(rel) and not (van.exists() and h == sha1(van)):
                edited.append(rel)
    if edited:
        print("\n".join(edited[:20]), file=sys.stderr)
        raise SystemExit(f"{target}: {len(edited)} file(s) were edited since the last assemble; pass --force to overwrite them")

    for rel, im in files.items():
        dst = out_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        im.save(dst)
        index[rel] = sha1(dst)
    index_path.write_text(json.dumps(dict(sorted(index.items())), indent=0) + "\n")

    # The vanilla icons the extractor copied in are no longer referenced.
    m = manifest(target)
    for role in ("icon", "corpse_icon"):
        old = out_root / m["icons"][role]["file"]
        if old.exists() and old.name.startswith(("small-biter", "medium-biter")):
            old.unlink()
        m["icons"][role] = {"file": f"icons/{target}{'' if role == 'icon' else '-corpse'}.png", "icon_size": 64, "mipmap_count": 4}
    m["source"] = f"generated by tools/gen-art.py from art/{target}/generated/ (OpenAI image edits over vanilla biter poses)"
    m.pop("baked_tints", None)
    for anim, (params, iou) in fits.items():
        m["animations"][anim]["shadow"]["derived"] = {"from": "colour alpha", "shear_scale_offset": [round(v, 3) for v in params], "vanilla_iou": round(iou, 3)}
    m["animations"]["decay"]["derived"] = "dissolve of the last die frame"
    (out_root / "manifest.json").write_text(json.dumps(m, indent=2) + "\n")

    (out_root / "previews").mkdir(exist_ok=True)
    for anim, by_dir in colors.items():
        spec = vm["animations"][anim]
        preview(directions, by_dir, shadows.get(anim) or None, spec["color"], spec.get("shadow")).save(out_root / "previews" / f"{anim}.png")

    for line in report:
        print(f"  {line}")
    print(f"{target}: wrote {len(files)} files to {out_root.relative_to(ROOT)}")
    drift = colour_drift(colors)
    if drift:
        for sheet, frames in sorted(drift.items()):
            print(f"  colour drift: {sheet} frames {', '.join(map(str, frames))}")
        print(f"  look at them, then re-roll with: uv run tools/gen-art.py generate --target {target} --redo --only {' '.join(sorted(drift))}")


def cmd_review(args):
    """Animated GIFs of the assembled art/, all 16 directions per animation, so the
    motion can be judged without starting the game. GIF's 256-colour palette makes
    them a preview, not a reference."""
    out = BUILD / "review"
    out.mkdir(parents=True, exist_ok=True)
    for target in ([args.target] if args.target else list(CREATURES)):
        m = manifest(target)
        for anim, spec in m["animations"].items():
            cmeta, smeta = spec["color"], spec.get("shadow")
            rects = [entity_origin(cmeta) + (cmeta["width"], cmeta["height"])]
            if smeta:
                rects.append(entity_origin(smeta) + (smeta["width"], smeta["height"]))
            left = min(r[0] for r in rects); top = min(r[1] for r in rects)
            pw = int(round(max(r[0] + r[2] for r in rects) - left)); ph = int(round(max(r[1] + r[3] for r in rects) - top))
            pmeta = {"width": pw, "height": ph, "shift_by_pixel": [(left + pw / 2) / 2, (top + ph / 2) / 2]}
            cell = (round(pw * args.scale), round(ph * args.scale))
            frames = []
            for f in range(spec["frames_per_direction"]):
                sheet = Image.new("RGBA", (cell[0] * 4, cell[1] * 4), PREVIEW_BACKGROUND)
                for i, d in enumerate(m["directions"]):
                    frame = Image.new("RGBA", (pw, ph))
                    if smeta:
                        sh = np.asarray(Image.open(ART / target / anim / "shadow" / d / f"{f:02d}.png").convert("RGBA")).astype(np.float32) / 255
                        sh = place_into(sh, smeta, pmeta)
                        sh[..., 3] *= PREVIEW_SHADOW_ALPHA
                        frame.alpha_composite(to_image(sh))
                    col = np.asarray(Image.open(ART / target / anim / "color" / d / f"{f:02d}.png").convert("RGBA")).astype(np.float32) / 255
                    frame.alpha_composite(to_image(place_into(col, cmeta, pmeta)))
                    sheet.alpha_composite(frame.resize(cell, Image.LANCZOS), ((i % 4) * cell[0], (i // 4) * cell[1]))
                frames.append(sheet.convert("RGB"))
            # Factorio runs at 60 ticks/s; biters advance roughly one frame per 2-3 ticks.
            frames[0].save(out / f"{target}-{anim}.gif", save_all=True, append_images=frames[1:], duration=50, loop=0)
    print(f"wrote {out.relative_to(ROOT)}/*.gif")


def cmd_assemble(args):
    ensure_vanilla()
    for t in ([args.target] if args.target else list(CREATURES)):
        assemble_target(t, args.force)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, only=True):
        p.add_argument("--target", choices=list(CREATURES))
        if only:
            p.add_argument("--only", nargs="*", help="anim or anim/direction, e.g. run/06-SE die")

    common(sub.add_parser("status"), only=False)
    common(sub.add_parser("guides"))
    g = sub.add_parser("generate")
    common(g)
    g.add_argument("--redo", action="store_true", help="regenerate sheets that already exist")
    g.add_argument("--model", default=DEFAULT_MODEL)
    g.add_argument("--quality", default=DEFAULT_QUALITY)
    g.add_argument("--jobs", type=int, default=4, help="concurrent requests")
    g.add_argument("--dry-run", action="store_true", help="build the requests and print them, send nothing")
    a = sub.add_parser("anchor")
    a.add_argument("--target", choices=list(CREATURES), required=True)
    a.add_argument("--from", dest="source", default="run/06-SE")
    a.add_argument("--frame", type=int, default=0)
    s = sub.add_parser("assemble")
    s.add_argument("--target", choices=list(CREATURES))
    s.add_argument("--force", action="store_true", help="overwrite frames edited by hand since the last assemble")

    r = sub.add_parser("review")
    r.add_argument("--target", choices=list(CREATURES))
    r.add_argument("--scale", type=float, default=0.5, help="of the sheet pixels; the game shows 0.25 (small) or 0.35 (medium)")

    args = ap.parse_args()
    {"review": cmd_review, "status": cmd_status, "guides": cmd_guides, "generate": cmd_generate, "anchor": cmd_anchor, "assemble": cmd_assemble}[args.cmd](args)


if __name__ == "__main__":
    main()
