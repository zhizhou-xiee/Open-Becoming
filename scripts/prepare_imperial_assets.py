#!/usr/bin/env python3
"""Prepare the local artwork used by the ``登基`` appearance theme.

The source folder contains a mix of already-transparent portraits and artwork
rendered on white.  This script keeps the original artwork untouched, removes
only the white area connected to an image edge, trims excess padding, and
writes web-friendly local assets under ``static/imperial``.
"""

from __future__ import annotations

import argparse
from collections import deque
from pathlib import Path

from PIL import Image, ImageDraw


PORTRAITS = {
    "char6": "1.png",
    "char5": "2.png",
    "char2": "3.png",
    "char4": "4.png",
    "char1": "5.png",
    "char3": "6.png",
}

WHITE_BACKED = {
    "plaque.png": "head.jpg",
    "edict-composer.png": "输入框.JPG",
    "nav-court.png": "底部导航:列表.PNG",
    "nav-council.png": "底部导航:群聊.PNG",
    "nav-memory.png": "底部导航:记忆.PNG",
    "nav-garden.png": "底部导航:猫窝.PNG",
    "nav-seal.png": "底部导航:设置+发送键.PNG",
}


def _edge_connected_white_to_alpha(image: Image.Image, threshold: int = 82) -> Image.Image:
    """Remove the nearly-white background without erasing enclosed parchment."""
    rgba = image.convert("RGBA")
    width, height = rgba.size
    work = rgba.convert("RGB")
    marker = (255, 0, 255)
    draw = ImageDraw.Draw(work)
    seeds = {
        (0, 0),
        (width - 1, 0),
        (0, height - 1),
        (width - 1, height - 1),
        (width // 2, 0),
        (width // 2, height - 1),
        (0, height // 2),
        (width - 1, height // 2),
    }
    for seed in seeds:
        pixel = work.getpixel(seed)
        if min(pixel) >= 215:
            ImageDraw.floodfill(work, seed, marker, thresh=threshold)

    source_pixels = rgba.load()
    work_pixels = work.load()
    for y in range(height):
        for x in range(width):
            if work_pixels[x, y] == marker:
                source_pixels[x, y] = (255, 255, 255, 0)
    return rgba


def _trim(image: Image.Image, padding: int = 18) -> Image.Image:
    alpha = image.getchannel("A")
    box = alpha.getbbox()
    if not box:
        return image
    left, top, right, bottom = box
    left = max(0, left - padding)
    top = max(0, top - padding)
    right = min(image.width, right + padding)
    bottom = min(image.height, bottom + padding)
    return image.crop((left, top, right, bottom))


def _keep_largest_alpha_component(image: Image.Image, cutoff: int = 10) -> Image.Image:
    """Drop isolated dust left behind by white-background extraction."""
    rgba = image.convert("RGBA")
    alpha = rgba.getchannel("A")
    width, height = rgba.size
    pixels = alpha.load()
    seen = bytearray(width * height)
    largest: list[int] = []

    for y in range(height):
        for x in range(width):
            start = y * width + x
            if seen[start] or pixels[x, y] <= cutoff:
                continue
            seen[start] = 1
            queue = deque([start])
            component: list[int] = []
            while queue:
                current = queue.popleft()
                component.append(current)
                cy, cx = divmod(current, width)
                for nx, ny in ((cx - 1, cy), (cx + 1, cy), (cx, cy - 1), (cx, cy + 1)):
                    if nx < 0 or ny < 0 or nx >= width or ny >= height:
                        continue
                    index = ny * width + nx
                    if not seen[index] and pixels[nx, ny] > cutoff:
                        seen[index] = 1
                        queue.append(index)
            if len(component) > len(largest):
                largest = component

    keep = bytearray(width * height)
    for index in largest:
        keep[index] = 1
    rgba_pixels = rgba.load()
    for y in range(height):
        row = y * width
        for x in range(width):
            if not keep[row + x]:
                red, green, blue, _ = rgba_pixels[x, y]
                rgba_pixels[x, y] = (red, green, blue, 0)
    return rgba


def _fit(image: Image.Image, max_edge: int) -> Image.Image:
    if max(image.size) <= max_edge:
        return image
    copy = image.copy()
    copy.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    return copy


def _save_webp(
    image: Image.Image,
    path: Path,
    *,
    quality: int = 88,
    lossless: bool = False,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, "WEBP", quality=quality, method=6, lossless=lossless)


def _save_png(image: Image.Image, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, "PNG", optimize=True, compress_level=9)


def prepare(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)

    for character_id, filename in PORTRAITS.items():
        portrait = Image.open(source / filename).convert("RGBA")
        portrait = _fit(_trim(portrait, padding=8), 900)
        _save_webp(portrait, target / f"portrait-{character_id}.webp", quality=91)

    backgrounds = {
        "court.webp": source / "列表背景.PNG",
        "bedroom.webp": source / "单聊:群聊背景.PNG",
    }
    for output, path in backgrounds.items():
        background = _fit(Image.open(path).convert("RGB"), 1800)
        _save_webp(background, target / output, quality=86)

    for output, filename in WHITE_BACKED.items():
        artwork = Image.open(source / filename)
        artwork = _edge_connected_white_to_alpha(artwork)
        artwork = _keep_largest_alpha_component(artwork)
        max_edge = 980 if output in {"plaque.png", "edict-composer.png"} else 440
        artwork = _fit(_trim(artwork), max_edge)
        _save_png(artwork, target / output)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    args = parser.parse_args()
    prepare(args.source, args.target)


if __name__ == "__main__":
    main()
