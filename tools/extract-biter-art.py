#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["numpy", "pillow"]
# ///
"""Extract the vanilla small and medium biter sprites as per-frame PNGs for repainting.

    uv run tools/extract-biter-art.py [--data <factorio data dir>] [--out art] [--force]

Factorio has no small-biter or medium-biter artwork as such. All four biter tiers
draw the same sheets in __base__/graphics/entity/biter (a base layer, two mask
layers and a shadow), and a tier is only a scale plus two tint colours applied to
the masks (see prototypes/entity/biter-animations.lua and enemy-constants.lua).
This script bakes each tier's tint into the frames so they look like the creature
in game, which also means the replacement art can be a single colour layer with
no masks.

Output, per target:
    <anim>/color/<NN-DIR>/<frame>.png    base + tinted masks, on one shared canvas
    <anim>/shadow/<NN-DIR>/<frame>.png   untouched shadow layer (decay has none)
    previews/<anim>.png                  contact sheet: one row per direction
    icons/                               the tier's own icon and corpse icon
    manifest.json                        canvas sizes, shifts and frame counts
"""

import argparse
import json
import re
import shutil
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

STEAM_DATA = (
    Path.home()
    / "Library/Application Support/Steam/steamapps/common/Factorio/factorio.app/Contents/data"
)

TARGETS = {
    "boxelder-bug": "small",
    "asian-beetle": "medium",
}

# Frame counts are written in biter-animations.lua rather than in the sheets'
# metadata, and can't be recovered from image sizes because the last row of a
# sheet may be partly empty.
ANIMATIONS = {
    "run": {"frames": 16, "shadow": True},
    "attack": {"frames": 11, "shadow": True},
    "die": {"frames": 17, "shadow": True},
    "decay": {"frames": 24, "shadow": False},
}

# RotatedAnimation direction 0 is north, turning clockwise.
DIRECTIONS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
              "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]

PREVIEW_SCALE = 0.25
PREVIEW_LABEL_WIDTH = 56
PREVIEW_BACKGROUND = (111, 125, 92, 255)   # roughly Nauvis grass, so shadows read
PREVIEW_SHADOW_ALPHA = 0.5


@dataclass
class Sheet:
    """One layer of a biter animation, possibly split across several PNGs."""

    width: int
    height: int
    center: tuple[int, int]   # frame centre relative to the entity, in sheet pixels
    columns: int
    per_file: int
    files: list[np.ndarray]

    @classmethod
    def load(cls, biter_dir: Path, name: str, frame_total: int) -> "Sheet":
        meta = (biter_dir / f"{name}.lua").read_text()
        width = int(re.search(r"width\s*=\s*(\d+)", meta)[1])
        height = int(re.search(r"height\s*=\s*(\d+)", meta)[1])
        sx, sy = re.search(r"util\.by_pixel\(\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*\)", meta).groups()
        lines_per_file = int(re.search(r"lines_per_file\s*=\s*(\d+)", meta)[1])
        suffixes = re.findall(r'"(-\d+\.png)"', meta)

        # by_pixel() is in 1x pixels, but the sheets are drawn at scale 0.5, so one
        # 1x pixel of shift is two sheet pixels. Every shift in these sheets lands on
        # a whole sheet pixel; the assert catches a future one that doesn't.
        center = (float(sx) * 2, float(sy) * 2)
        assert all(c.is_integer() for c in center), f"{name}: half-pixel shift {center}"

        files = [np.asarray(Image.open(biter_dir / f"{name}{s}").convert("RGBA")) for s in suffixes]

        # The column count is the `slice` value in biter-animations.lua, which can
        # differ from the line_length in the .lua next to the sheet (attack says 16
        # but is laid out 11 wide). The image width is the reliable source.
        columns, rem = divmod(files[0].shape[1], width)
        assert rem == 0, f"{name}: sheet width is not a multiple of the frame width"
        for img in files[:-1]:
            assert img.shape[0] == lines_per_file * height, f"{name}: short sheet before the last file"
        slots = sum(img.shape[0] // height for img in files) * columns
        assert frame_total <= slots < frame_total + columns, f"{name}: {slots} slots for {frame_total} frames"

        return cls(width, height, (int(center[0]), int(center[1])), columns, lines_per_file * columns, files)

    @property
    def rect(self) -> tuple[int, int, int, int]:
        left = self.center[0] - self.width // 2
        top = self.center[1] - self.height // 2
        return left, top, left + self.width, top + self.height

    def frame(self, index: int) -> np.ndarray:
        # Frames run direction by direction (all of direction 0, then direction 1),
        # filling rows left to right and spilling into the next file.
        file_index, local = divmod(index, self.per_file)
        row, col = divmod(local, self.columns)
        y, x = row * self.height, col * self.width
        return self.files[file_index][y : y + self.height, x : x + self.width]


def union(rects):
    lefts, tops, rights, bottoms = zip(*rects)
    return min(lefts), min(tops), max(rights), max(bottoms)


def premultiply(px: np.ndarray, tint=(1, 1, 1, 1), overlay=False):
    rgba = px.astype(np.float32) / 255
    rgb, a = rgba[..., :3], rgba[..., 3:]
    t = np.asarray(tint, np.float32)
    if overlay:
        rgb = np.where(rgb < 0.5, 2 * rgb * t[:3], 1 - 2 * (1 - rgb) * (1 - t[:3]))
    else:
        rgb = rgb * t[:3]
    # Factorio's exact blend for tint_as_overlay and for tint alpha below 1 is not
    # documented. This treats tint alpha as plain opacity, which is close enough to
    # judge colour and pose by eye but is not a pixel match for the game.
    a = a * t[3]
    return rgb * a, a


def place(canvas_rect, layer_rect, rgb, a, src_rgb, src_a):
    x, y = layer_rect[0] - canvas_rect[0], layer_rect[1] - canvas_rect[1]
    h, w = src_a.shape[:2]
    region = (slice(y, y + h), slice(x, x + w))
    rgb[region] = src_rgb + rgb[region] * (1 - src_a)
    a[region] = src_a + a[region] * (1 - src_a)


def to_image(rgb, a) -> Image.Image:
    with np.errstate(divide="ignore", invalid="ignore"):
        straight = np.where(a > 0, rgb / a, 0)
    out = np.concatenate([straight, a], axis=-1)
    return Image.fromarray(np.clip(np.rint(out * 255), 0, 255).astype(np.uint8), "RGBA")


def tier_constants(base: Path, tier: str) -> dict:
    src = (base / "prototypes/entity/enemy-constants.lua").read_text()

    def tint(n):
        values = re.search(rf"{tier}_biter_tint{n}\s*=\s*\{{([^}}]*)\}}", src)[1]
        return [float(v) for v in values.split(",")]

    scale = float(re.search(rf"{tier}_biter_scale\s*=\s*([\d.]+)", src)[1])
    return {"scale": scale, "tint1": tint(1), "tint2": tint(2)}


def extract_animation(base: Path, out: Path, anim: str, spec: dict, tiers: dict) -> dict:
    biter_dir = base / "graphics/entity/biter"
    total = len(DIRECTIONS) * spec["frames"]
    layers = [Sheet.load(biter_dir, f"biter-{anim}{suffix}", total) for suffix in ("", "-mask1", "-mask2")]
    shadow = Sheet.load(biter_dir, f"biter-{anim}-shadow", total) if spec["shadow"] else None

    # The masks hang outside the base frame in places (mask2 of run sits 3px above
    # it), so the colour canvas is the union of all three layers rather than the
    # base layer's own frame.
    color_rect = union(layer.rect for layer in layers)
    preview_rect = union([color_rect] + ([shadow.rect] if shadow else []))
    cw, ch = color_rect[2] - color_rect[0], color_rect[3] - color_rect[1]
    pw, ph = preview_rect[2] - preview_rect[0], preview_rect[3] - preview_rect[1]
    cell = (round(pw * PREVIEW_SCALE), round(ph * PREVIEW_SCALE))

    entries = {}
    for target, tier in TARGETS.items():
        consts = tiers[tier]
        root = out / target / anim
        preview = Image.new("RGBA", (PREVIEW_LABEL_WIDTH + cell[0] * spec["frames"], cell[1] * len(DIRECTIONS)), PREVIEW_BACKGROUND)
        draw = ImageDraw.Draw(preview)

        for d, label in enumerate(DIRECTIONS):
            dir_name = f"{d:02d}-{label}"
            (root / "color" / dir_name).mkdir(parents=True, exist_ok=True)
            if shadow:
                (root / "shadow" / dir_name).mkdir(parents=True, exist_ok=True)
            draw.text((8, d * cell[1] + cell[1] // 2 - 6), dir_name, fill=(255, 255, 255, 255))

            for f in range(spec["frames"]):
                index = d * spec["frames"] + f
                rgb = np.zeros((ch, cw, 3), np.float32)
                a = np.zeros((ch, cw, 1), np.float32)
                place(color_rect, layers[0].rect, rgb, a, *premultiply(layers[0].frame(index)))
                place(color_rect, layers[1].rect, rgb, a, *premultiply(layers[1].frame(index), consts["tint1"]))
                place(color_rect, layers[2].rect, rgb, a, *premultiply(layers[2].frame(index), consts["tint2"], overlay=True))
                to_image(rgb, a).save(root / "color" / dir_name / f"{f:02d}.png")

                prgb = np.zeros((ph, pw, 3), np.float32)
                pa = np.zeros((ph, pw, 1), np.float32)
                if shadow:
                    shadow_px = shadow.frame(index)
                    Image.fromarray(shadow_px, "RGBA").save(root / "shadow" / dir_name / f"{f:02d}.png")
                    sa = shadow_px[..., 3:].astype(np.float32) / 255 * PREVIEW_SHADOW_ALPHA
                    place(preview_rect, shadow.rect, prgb, pa, np.zeros_like(sa).repeat(3, -1), sa)
                place(preview_rect, color_rect, prgb, pa, rgb, a)
                thumb = to_image(prgb, pa).resize(cell, Image.LANCZOS)
                preview.alpha_composite(thumb, (PREVIEW_LABEL_WIDTH + f * cell[0], d * cell[1]))

        (out / target / "previews").mkdir(parents=True, exist_ok=True)
        preview.save(out / target / "previews" / f"{anim}.png")

        entry = {
            "frames_per_direction": spec["frames"],
            "color": frame_meta(color_rect),
            "replaces_layers": [f"biter-{anim}", f"biter-{anim}-mask1", f"biter-{anim}-mask2"],
        }
        if shadow:
            entry["shadow"] = frame_meta(shadow.rect)
        entries[target] = entry
    return entries


def frame_meta(rect) -> dict:
    left, top, right, bottom = rect
    w, h = right - left, bottom - top
    # Back to the 1x by_pixel() units the vanilla sheet .lua files use, so a pack
    # step can write util.by_pixel(x, y) and pass multiply_shift = tier scale.
    return {"width": w, "height": h, "shift_by_pixel": [(left + w / 2) / 2, (top + h / 2) / 2]}


def copy_icons(base: Path, out: Path, target: str, tier: str) -> dict:
    icons = out / target / "icons"
    icons.mkdir(parents=True, exist_ok=True)
    meta = {}
    for role, name in (("icon", f"{tier}-biter"), ("corpse_icon", f"{tier}-biter-corpse")):
        src = base / "graphics/icons" / f"{name}.png"
        shutil.copyfile(src, icons / f"{name}.png")
        # 2.0 icons are a 64px image with its mipmaps laid out to the right
        # (64 + 32 + 16 + 8 = 120), not a plain 64x64.
        width = Image.open(src).width
        mips, size = 0, 64
        while width > 0 and size >= 1:
            width -= size
            size //= 2
            mips += 1
        meta[role] = {"file": f"icons/{name}.png", "icon_size": 64, "mipmap_count": mips}
    return meta


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data", type=Path, default=STEAM_DATA, help="Factorio's data/ directory")
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parent.parent / "art")
    parser.add_argument("--force", action="store_true", help="replace existing output, discarding any edits in it")
    args = parser.parse_args()

    base = args.data / "base"
    version = json.loads((base / "info.json").read_text())["version"]

    # These folders become the hand-edited source art, so a casual re-run must not
    # overwrite repainted frames with vanilla ones.
    for target in TARGETS:
        existing = args.out / target
        if existing.exists():
            if not args.force:
                raise SystemExit(f"{existing} already exists; pass --force to discard it and re-extract")
            shutil.rmtree(existing)

    tiers = {tier: tier_constants(base, tier) for tier in TARGETS.values()}
    with ProcessPoolExecutor() as pool:
        jobs = {anim: pool.submit(extract_animation, base, args.out, anim, spec, tiers) for anim, spec in ANIMATIONS.items()}
        results = {anim: job.result() for anim, job in jobs.items()}

    for target, tier in TARGETS.items():
        manifest = {
            "replaces": f"{tier}-biter",
            "corpse": f"{tier}-biter-corpse",
            "source": f"Factorio {version}, __base__/graphics/entity/biter",
            "scale": tiers[tier]["scale"],
            "scale_note": "layers are drawn at scale * 0.5 with multiply_shift = scale",
            "baked_tints": {"mask1": tiers[tier]["tint1"], "mask2_overlay": tiers[tier]["tint2"]},
            "directions": [f"{d:02d}-{label}" for d, label in enumerate(DIRECTIONS)],
            "direction_order": "index 0 faces north, then clockwise",
            "animations": {anim: entries[target] for anim, entries in results.items()},
            "icons": copy_icons(base, args.out, target, tier),
        }
        (args.out / target / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        print(f"{target}: {args.out / target}")


if __name__ == "__main__":
    main()
