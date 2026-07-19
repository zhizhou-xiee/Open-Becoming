#!/usr/bin/env python3
"""Extract Finder custom-icon PNGs into Becoming cover patch files."""

import argparse
import struct
from pathlib import Path


def largest_png_from_icns(data):
    start = data.find(b"icns", 0, min(len(data), 4096))
    if start < 0 or start + 8 > len(data):
        return None
    total = struct.unpack(">I", data[start + 4:start + 8])[0]
    end = min(len(data), start + total)
    cursor = start + 8
    choices = []
    while cursor + 8 <= end:
        size = struct.unpack(">I", data[cursor + 4:cursor + 8])[0]
        if size < 8 or cursor + size > end:
            break
        payload = data[cursor + 8:cursor + size]
        if payload.startswith(b"\x89PNG\r\n\x1a\n") and len(payload) >= 24:
            width, height = struct.unpack(">II", payload[16:24])
            choices.append((width * height, payload))
        cursor += size
    return max(choices, default=(0, None), key=lambda item: item[0])[1]


def resource_fork(path):
    try:
        with open(f"{path}/..namedfork/rsrc", "rb") as handle:
            return handle.read()
    except OSError:
        return b""


def extract_directory(source, output):
    output.mkdir(parents=True, exist_ok=True)
    extracted = 0
    for audio in sorted(source.iterdir()):
        if not audio.is_file() or audio.suffix.lower() not in {".ogg", ".opus"}:
            continue
        image = largest_png_from_icns(resource_fork(audio))
        if not image:
            continue
        (output / f"{audio.stem}.cover.png").write_bytes(image)
        extracted += 1
    return extracted


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or args.source / "OGG封面补丁"
    count = extract_directory(args.source, output)
    print(f"extracted {count} cover(s) to {output}")


if __name__ == "__main__":
    main()
