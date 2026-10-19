import os
import sys

import numpy as np

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

LOAD_ERRORS = []  # files that could not be loaded, filled by get_available_palettes()


def get_default_vga_palette():
    # Standard 16-color EGA/CGA baseline (BGR format)
    base_16 = [
        [0, 0, 0],       [170, 0, 0],     [0, 170, 0],     [170, 170, 0],
        [0, 0, 170],     [170, 0, 170],   [0, 85, 170],    [170, 170, 170],
        [85, 85, 85],    [255, 85, 85],   [85, 255, 85],   [255, 255, 85],
        [85, 85, 255],   [255, 85, 255],  [85, 255, 255],  [255, 255, 255]
    ]
    palette = list(base_16)

    # 16-level grayscale ramp
    for i in range(16):
        val = int((i / 15.0) * 255)
        palette.append([val, val, val])

    # 216-color RGB cube
    steps = [0, 51, 102, 153, 204, 255]
    for r in steps:
        for g in steps:
            for b in steps:
                if len(palette) < 256:
                    palette.append([b, g, r])

    while len(palette) < 256:
        palette.append([0, 0, 0])

    return np.array(palette, dtype=np.float32)


def get_prince_dungeon_palette():
    # Verified Prince of Persia Dungeon 16-color VGA DAC palette (BGR format)
    dungeon_16 = [
        [0, 0, 0],         # 0: Black
        [120, 120, 120],   # 1: Stone Gray
        [168, 168, 168],   # 2: Light Stone Gray
        [72, 72, 72],      # 3: Dark Stone Shadow
        [0, 120, 168],     # 4: Torch Wood / Brown
        [48, 168, 216],    # 5: Bright Wood / Highlight
        [0, 72, 120],      # 6: Dark Brown
        [0, 32, 72],       # 7: Deep Brown
        [248, 248, 248],   # 8: White (Prince Tunic)
        [136, 136, 136],   # 9: Tunic Gray
        [72, 72, 72],      # 10: Deep Tunic Shadow
        [0, 104, 248],     # 11: Potion Orange / Torch Red
        [48, 168, 248],    # 12: Bright Torch Flame
        [0, 48, 152],      # 13: Flame Dark Red
        [136, 168, 248],   # 14: Skin Tone
        [88, 104, 200]     # 15: Skin Shadow
    ]
    palette = np.zeros((256, 3), dtype=np.float32)
    for idx, col in enumerate(dungeon_16):
        palette[idx] = col
    for i in range(16, 256):
        palette[i] = palette[i % 16]
    return palette


def get_prince_palace_palette():
    # Verified Prince of Persia Palace (PV) 16-color VGA DAC palette (BGR format)
    palace_16 = [
        [0, 0, 0],         # 0: Transparent / Black
        [216, 168, 88],    # 1: Palace Wall Blue / Slate
        [248, 216, 152],   # 2: Light Blue Wall Highlight
        [184, 120, 48],    # 3: Dark Blue Shadow
        [56, 104, 216],    # 4: Gold / Sandstone Pillar
        [104, 152, 248],   # 5: Bright Gold / Floor Light
        [32, 56, 152],     # 6: Dark Amber / Floor Shadow
        [16, 24, 88],      # 7: Deep Shadow
        [248, 248, 248],   # 8: White (Prince Tunic)
        [136, 136, 136],   # 9: Gray Tunic Shadow
        [72, 72, 72],      # 10: Dark Gray Shadow
        [0, 40, 200],      # 11: Red Sash / Carpet Base
        [48, 104, 248],    # 12: Light Red / Bright Carpet
        [0, 16, 120],      # 13: Dark Crimson Shadow
        [136, 168, 248],   # 14: Skin / Face Tone
        [88, 104, 200]     # 15: Skin Shadow
    ]
    palette = np.zeros((256, 3), dtype=np.float32)
    for idx, col in enumerate(palace_16):
        palette[idx] = col
    for i in range(16, 256):
        palette[i] = palette[i % 16]
    return palette


def parse_palette_file(data):
    """
    Reads a palette file and returns a list of (r, g, b) tuples with 8-bit values.
    Supported formats (detected automatically):
      * JASC-PAL (Paint Shop Pro, text)
      * RIFF PAL (Microsoft, binary)
      * GIMP palette (.gpl text, also accepted with a .pal name)
      * Raw RGB triplets, as dumped from a game or DOSBox: 6-bit VGA DAC values (0-63)
        or 8-bit values (0-255), up to 256 colors
    Raises ValueError if the file is not a palette.
    """
    if data.startswith(b"JASC-PAL"):
        lines = data.decode("ascii", "replace").split()
        count = int(lines[2])
        values = [int(v) for v in lines[3:3 + count * 3]]
        return [tuple(values[i:i + 3]) for i in range(0, len(values) - 2, 3)]

    if data.startswith(b"RIFF") and data[8:12] == b"PAL ":
        pos = data.find(b"data", 12)
        if pos < 0:
            raise ValueError("RIFF palette without a data chunk")
        count = int.from_bytes(data[pos + 10:pos + 12], "little")
        entries = data[pos + 12:pos + 12 + count * 4]
        return [tuple(entries[i:i + 3]) for i in range(0, len(entries) - 3, 4)]

    if data.startswith(b"GIMP Palette"):
        colors = []
        for line in data.decode("utf-8", "replace").splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 3 and all(p.isdigit() for p in parts[:3]):
                colors.append(tuple(int(p) for p in parts[:3]))
        return colors

    # Raw RGB triplets, possibly with a game-specific header or trailer
    if len(data) <= 768:
        if len(data) < 3 or len(data) % 3 != 0:
            raise ValueError(f"unknown format ({len(data)} bytes)")
        block = data
    else:
        block = find_palette_block(data)
    raw = np.frombuffer(block, dtype=np.uint8).reshape(-1, 3)
    if raw.max() <= 63:
        raw = raw.astype(np.uint16) * 255 // 63  # 6-bit VGA DAC values -> 8-bit
    return [tuple(int(v) for v in rgb) for rgb in raw]


def find_palette_block(data, size=768):
    """
    Finds the 256-color block in a file that has extra bytes around it, like game palettes
    with their own header (782 bytes = 14-byte header + 768). VGA DAC values are 6-bit, so the
    block where every byte is 0-63 is the palette. A header at the start is the most common case,
    so the block at the end is tried first, then the one at the start, then everything in between.
    """
    last = len(data) - size
    for offset in [last, 0] + list(range(1, last)):
        block = data[offset:offset + size]
        if max(block) <= 63:
            return block
    return data[last:]  # no 6-bit block: assume 8-bit values after a header


def load_raw_palette_file(filepath):
    """Loads a palette file (any supported format) as a 256-color BGR array."""
    with open(filepath, "rb") as f:
        colors = parse_palette_file(f.read())[:256]
    if not colors:
        raise ValueError("no colors in file")

    palette = np.zeros((256, 3), dtype=np.float32)
    for i, (r, g, b) in enumerate(colors):
        palette[i] = [min(b, 255), min(g, 255), min(r, 255)]

    # Fewer than 256 colors (e.g. a 16-color EGA palette): repeat them
    count = len(colors)
    for i in range(count, 256):
        palette[i] = palette[i % count]

    return palette


def get_available_palettes():
    # Dictionary mapping display names to palette arrays
    registry = {
        "Default BIOS (Standard VGA)": get_default_vga_palette(),
        "Prince of Persia - Dungeon": get_prince_dungeon_palette(),
        "Prince of Persia - Palace": get_prince_palace_palette(),
    }

    # Discover and add palette files located in the data directory
    LOAD_ERRORS.clear()
    if os.path.exists(DATA_DIR):
        for fname in sorted(os.listdir(DATA_DIR), key=str.lower):
            if fname.lower().endswith((".pal", ".gpl")):
                fpath = os.path.join(DATA_DIR, fname)
                try:
                    registry[f"File: {fname}"] = load_raw_palette_file(fpath)
                except Exception as e:
                    # Report instead of hiding: the launcher writes this to logs/<tool>.log
                    LOAD_ERRORS.append(f"{fname}: {e}")
                    print(f"Palette skipped: {fname}: {e}", file=sys.stderr)

    return registry