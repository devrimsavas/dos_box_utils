import os
import numpy as np

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")


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


def load_raw_palette_file(filepath):
    # Reads 48 or 768 byte raw VGA DAC files (6-bit RGB)
    with open(filepath, "rb") as f:
        data = f.read()

    palette = np.zeros((256, 3), dtype=np.float32)
    count = len(data) // 3

    for i in range(count):
        # Scale 6-bit DAC values (0..63) to 8-bit range (0..255)
        r = (data[i * 3 + 0] & 0x3F) << 2
        g = (data[i * 3 + 1] & 0x3F) << 2
        b = (data[i * 3 + 2] & 0x3F) << 2
        palette[i] = [b, g, r]

    for i in range(count, 256):
        palette[i] = palette[i % max(1, count)]

    return palette


def get_available_palettes():
    # Dictionary mapping display names to palette arrays
    registry = {
        "Default BIOS (Standard VGA)": get_default_vga_palette(),
        "Prince of Persia - Dungeon": get_prince_dungeon_palette(),
        "Prince of Persia - Palace": get_prince_palace_palette(),
    }

    # Discover and add any raw .pal files located in the data directory
    if os.path.exists(DATA_DIR):
        for fname in os.listdir(DATA_DIR):
            if fname.lower().endswith(".pal"):
                fpath = os.path.join(DATA_DIR, fname)
                try:
                    registry[f"File: {fname}"] = load_raw_palette_file(fpath)
                except Exception:
                    pass

    return registry