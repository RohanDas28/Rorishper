"""Rorishper: local push-to-talk voice typing for Windows."""

from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import gc
import io
import json
import math
import os
from pathlib import Path
import queue
import sysconfig
import threading
import time
import tkinter as tk
import webbrowser

import customtkinter as ctk
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageTk
import pystray
import sounddevice as sd
from pynput import keyboard

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "settings.json"
SAMPLE_RATE = 16_000
APP_NAME = "Rorishper"
IDLE_UNLOAD_SECONDS = 5 * 60

# Set Windows application identity before any UI is initialized.
# This prevents Windows taskbar grouping under generic python.exe and ensures custom app icon displays.
if os.name == "nt":
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("rohandas.rorishper.app.1.0")
    except Exception:
        pass

# ============================================================================
# Midnight Black Theme Palette
# ============================================================================
COLOR_MAIN_BG = "#0A0C12"        # Main background · Midnight Black
COLOR_COMP_BG = "#151727"        # Component background · Deep Navy
COLOR_WAVE_SHADOW = "#332B5D"    # Waveform shadows · Dark Purple
COLOR_BORDER_GLOW = "#5842D2"    # Border and glow · Electric Purple
COLOR_WAVE_HIGHLIGHT = "#666BDF" # Waveform highlights · Neon Blue
COLOR_WAVE_ACCENT = "#AB7EE1"    # Waveform accents · Soft Lavender
COLOR_TEXT_MAIN = "#EAEAF0"      # Text and icons · Cool White
COLOR_TEXT_MUTED = "#8E92AA"     # Secondary labels / captions
COLOR_HERO_BOX = "#10121F"       # Inner visualizer well
COLOR_BUTTON_BG = "#202338"      # Interactive control background
COLOR_BUTTON_HOVER = "#2C304A"   # Interactive control hover

RGB_MAIN_BG = (10, 12, 18)
RGB_COMP_BG = (21, 23, 39)
RGB_WAVE_SHADOW = (51, 43, 93)
RGB_BORDER_GLOW = (88, 66, 210)
RGB_WAVE_HIGHLIGHT = (102, 107, 223)
RGB_WAVE_ACCENT = (171, 126, 225)
RGB_TEXT_MAIN = (234, 234, 240)
RGB_HERO_BOX = (16, 18, 31)

ANIMATION_STYLES = [
    "Waveform (Audio-style)",
    "Glowing Bars (Capsule)",
    "Fluid Ribbon (Neon Wave)",
]

DEFAULTS = {
    "hotkey": ["alt", "shift"],
    "model": "large-v3-turbo",
    "paste": True,
    "idle_unload": True,
    "show_overlay": True,
    "animation_style": "Waveform (Audio-style)",
}

CUDA_DLL_HANDLES = []


def register_cuda_dll_dirs():
    """Expose NVIDIA runtime wheel DLLs to CTranslate2 on Windows."""
    nvidia_root = Path(sysconfig.get_paths()["purelib"]) / "nvidia"
    if not nvidia_root.is_dir() or not hasattr(os, "add_dll_directory"):
        return
    folders = []
    for folder in nvidia_root.rglob("bin"):
        if folder.is_dir() and any(folder.glob("*.dll")):
            try:
                CUDA_DLL_HANDLES.append(os.add_dll_directory(str(folder)))
                folders.append(str(folder))
            except OSError:
                continue
    # CTranslate2 Windows loader also needs these on PATH
    current = os.environ.get("PATH", "").split(os.pathsep)
    current_lower = {item.casefold() for item in current if item}
    missing = [folder for folder in folders if folder.casefold() not in current_lower]
    if missing:
        os.environ["PATH"] = os.pathsep.join(missing + current)


register_cuda_dll_dirs()
from faster_whisper import WhisperModel, download_model


def get_system_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    """Load Segoe UI or Arial from Windows fonts directory with safe fallback."""
    names = ["segoeuib.ttf", "segoeui.ttf", "arialbd.ttf", "arial.ttf"] if bold else ["segoeui.ttf", "arial.ttf"]
    win_fonts = Path(os.environ.get("WINDIR", "C:\\Windows")) / "Fonts"
    for name in names:
        p = win_fonts / name
        if p.exists():
            try:
                return ImageFont.truetype(str(p), size)
            except Exception:
                continue
    return ImageFont.load_default()


def load_settings():
    try:
        saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        settings = {**DEFAULTS, **saved}
        if settings.get("model") == "small":
            settings["model"] = "large-v3-turbo"
        if isinstance(settings["hotkey"], str):
            old = settings["hotkey"].lower()
            if "ctrl" in old:
                settings["hotkey"] = ["ctrl"]
            elif old.startswith("key."):
                settings["hotkey"] = [old]
            else:
                settings["hotkey"] = [old.strip("'")]
        if settings.get("animation_style") not in ANIMATION_STYLES:
            settings["animation_style"] = "Waveform (Audio-style)"
        return settings
    except (OSError, json.JSONDecodeError):
        return DEFAULTS.copy()


def nice_key(token):
    labels = {"ctrl": "Ctrl", "shift": "Shift", "alt": "Alt", "win": "Win", "space": "Space"}
    if token in labels:
        return labels[token]
    if token.startswith("Key."):
        return token[4:].replace("_l", " Left").replace("_r", " Right").title()
    return token.strip("'").upper()


# Direct hardware virtual-key mapping for Windows GetAsyncKeyState.
VK_MAP = {
    "ctrl": [0x11, 0xA2, 0xA3],
    "ctrl_l": [0xA2], "ctrl_r": [0xA3],
    "shift": [0x10, 0xA0, 0xA1],
    "shift_l": [0xA0], "shift_r": [0xA1],
    "alt": [0x12, 0xA4, 0xA5],
    "alt_l": [0xA4], "alt_r": [0xA5],
    "win": [0x5B, 0x5C],
    "cmd": [0x5B, 0x5C],
    "space": [0x20],
    "caps_lock": [0x14],
    "tab": [0x09],
    "enter": [0x0D],
    "escape": [0x1B], "esc": [0x1B],
    "backspace": [0x08],
    "insert": [0x2D], "delete": [0x2E],
    "home": [0x24], "end": [0x23],
    "page_up": [0x21], "page_down": [0x22],
    "up": [0x26], "down": [0x28], "left": [0x25], "right": [0x27],
}
for i in range(1, 25):
    VK_MAP[f"f{i}"] = [0x6F + i]

if os.name == "nt":
    try:
        ctypes.windll.user32.GetAsyncKeyState.restype = ctypes.c_short
        ctypes.windll.user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    except Exception:
        pass


def is_key_down(token: str) -> bool:
    """Check whether a key is physically held down using Windows GetAsyncKeyState."""
    tok = str(token).lower().strip()
    if tok.startswith("key."):
        tok = tok[4:]
    tok = tok.strip("'\"")
    if tok in VK_MAP:
        return any(bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000) for vk in VK_MAP[tok])
    if len(tok) == 1:
        vk = ctypes.windll.user32.VkKeyScanW(ord(tok)) & 0xFF
        if vk:
            return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)
    return False


# ============================================================================
# High-Quality Anti-Aliased Rendering Functions
# ============================================================================
# Cache for precomputed overlay base pill (gradient border, glow, and dark background)
_OVERLAY_BASE_CACHE: dict[tuple[int, int, int], Image.Image] = {}


def _get_overlay_base(w: int, h: int, scale: int = 2) -> Image.Image:
    key = (w, h, scale)
    if key in _OVERLAY_BASE_CACHE:
        return _OVERLAY_BASE_CACHE[key]

    sw, sh = w * scale, h * scale
    pad = 10 * scale
    rx1, ry1, rx2, ry2 = pad, pad, sw - pad, sh - pad
    radius = (ry2 - ry1) // 2
    border_w = int(2.2 * scale)

    base = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))

    # 1. Diffused outer glow in Electric Purple (#5842D2) with Soft Lavender highlight
    glow = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.rounded_rectangle(
        (rx1 - 3 * scale, ry1 - 3 * scale, rx2 + 3 * scale, ry2 + 3 * scale),
        radius=radius + 3 * scale,
        fill=(*RGB_BORDER_GLOW, 125),
    )
    # Right-side lavender accent glow
    gd.rounded_rectangle(
        (rx2 - 130 * scale, ry1 - 3 * scale, rx2 + 3 * scale, ry2 + 3 * scale),
        radius=radius + 3 * scale,
        fill=(*RGB_WAVE_ACCENT, 85),
    )
    glow = glow.filter(ImageFilter.GaussianBlur(7 * scale))
    base.paste(glow, (0, 0), glow)

    d = ImageDraw.Draw(base)

    # 2. Pill background: Deep Navy #151727 with high opacity
    d.rounded_rectangle((rx1, ry1, rx2, ry2), radius=radius, fill=(*RGB_COMP_BG, 248))

    # 3. Seamless gradient border: Neon Blue -> Electric Purple -> Soft Lavender
    grad = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
    gd_grad = ImageDraw.Draw(grad)
    for x in range(sw):
        t = x / max(1, sw - 1)
        if t < 0.45:
            u = t / 0.45
            r = int(RGB_WAVE_HIGHLIGHT[0] + (RGB_BORDER_GLOW[0] - RGB_WAVE_HIGHLIGHT[0]) * u)
            g = int(RGB_WAVE_HIGHLIGHT[1] + (RGB_BORDER_GLOW[1] - RGB_WAVE_HIGHLIGHT[1]) * u)
            b = int(RGB_WAVE_HIGHLIGHT[2] + (RGB_BORDER_GLOW[2] - RGB_WAVE_HIGHLIGHT[2]) * u)
        else:
            u = (t - 0.45) / 0.55
            r = int(RGB_BORDER_GLOW[0] + (RGB_WAVE_ACCENT[0] - RGB_BORDER_GLOW[0]) * u)
            g = int(RGB_BORDER_GLOW[1] + (RGB_WAVE_ACCENT[1] - RGB_BORDER_GLOW[1]) * u)
            b = int(RGB_BORDER_GLOW[2] + (RGB_WAVE_ACCENT[2] - RGB_BORDER_GLOW[2]) * u)
        gd_grad.line([(x, 0), (x, sh)], fill=(r, g, b, 255))

    mask = Image.new("L", (sw, sh), 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle((rx1, ry1, rx2, ry2), radius=radius, outline=255, width=border_w)
    base.paste(grad, (0, 0), mask)

    # Top specular rim line
    d.line([(rx1 + radius, ry1), (rx2 - radius, ry1)], fill=(245, 245, 255, 130), width=int(1.2 * scale))

    _OVERLAY_BASE_CACHE[key] = base
    return base


def render_hero_visualizer_image(w: int, h: int, style: str, state: str, frame: int, rms: float) -> Image.Image:
    """Render anti-aliased visualizer for the main window hero container."""
    scale = 2
    sw, sh = int(w * scale), int(h * scale)
    img = Image.new("RGBA", (sw, sh), (*RGB_HERO_BOX, 255))
    d = ImageDraw.Draw(img)

    pad = int(10 * scale)
    vw = sw - 2 * pad
    cy = sh // 2

    if style == "bars":
        num_bars = 13
        bar_w = int(5.5 * scale)
        gap = (vw - num_bars * bar_w) / max(1, num_bars - 1)
        max_h = (sh - 2 * pad) * 0.85
        for i in range(num_bars):
            bx = int(pad + i * (bar_w + gap))
            env = math.sin((i + 1) / (num_bars + 1) * math.pi)
            if state == "listening":
                osc = math.sin(frame * 0.22 + i * 0.75) * 0.3 + 0.7
                h_val = max(int(6 * scale), int(max_h * (0.22 + 0.78 * rms) * env * osc))
            elif state in ("transcribing", "loading", "waking"):
                osc = math.sin(frame * 0.18 - i * 0.5) * 0.35 + 0.65
                h_val = max(int(6 * scale), int(max_h * 0.60 * env * osc))
            else:
                osc = math.sin(frame * 0.08 + i * 0.35) * 0.2 + 0.8
                h_val = max(int(5 * scale), int(max_h * 0.32 * env * osc))

            t = i / max(1, num_bars - 1)
            r = int(RGB_WAVE_HIGHLIGHT[0] + (RGB_WAVE_ACCENT[0] - RGB_WAVE_HIGHLIGHT[0]) * t)
            g = int(RGB_WAVE_HIGHLIGHT[1] + (RGB_WAVE_ACCENT[1] - RGB_WAVE_HIGHLIGHT[1]) * t)
            b = int(RGB_WAVE_HIGHLIGHT[2] + (RGB_WAVE_ACCENT[2] - RGB_WAVE_HIGHLIGHT[2]) * t)
            d.rounded_rectangle((bx, cy - h_val // 2, bx + bar_w, cy + h_val // 2),
                                radius=bar_w // 2, fill=(r, g, b, 255))
            # Center subtle gloss dot
            highlight_h = max(int(3 * scale), int(h_val * 0.35))
            d.rounded_rectangle((bx + 1 * scale, cy - highlight_h // 2, bx + bar_w - 1 * scale, cy + highlight_h // 2),
                                radius=bar_w // 2, fill=(245, 245, 255, 140))

    elif style == "ribbon":
        xs = np.linspace(0, vw, int(vw))
        env = (0.5 * (1.0 - np.cos(2.0 * np.pi * xs / max(1, vw)))) ** 1.2
        energy = (0.2 + 0.8 * rms) if state == "listening" else 0.45
        max_amp = (sh - 2 * pad) * 0.50
        y_top = cy + np.sin(xs * 0.045 + frame * 0.14) * max_amp * energy * env
        y_bot = cy + np.sin(xs * 0.045 + frame * 0.14 + 1.2) * max_amp * energy * env * 0.7
        pts = [(pad + int(x), int(yt)) for x, yt in zip(xs, y_top)]
        pts += [(pad + int(x), int(yb)) for x, yb in reversed(list(zip(xs, y_bot)))]
        if len(pts) > 2:
            d.polygon(pts, fill=(*RGB_WAVE_SHADOW, 160))
        for i in range(len(xs) - 1):
            t = i / max(1, len(xs) - 1)
            r = int(RGB_WAVE_HIGHLIGHT[0] + (RGB_WAVE_ACCENT[0] - RGB_WAVE_HIGHLIGHT[0]) * t)
            g = int(RGB_WAVE_HIGHLIGHT[1] + (RGB_WAVE_ACCENT[1] - RGB_WAVE_HIGHLIGHT[1]) * t)
            b = int(RGB_WAVE_HIGHLIGHT[2] + (RGB_WAVE_ACCENT[2] - RGB_WAVE_HIGHLIGHT[2]) * t)
            d.line([(pad + int(xs[i]), int(y_top[i])), (pad + int(xs[i + 1]), int(y_top[i + 1]))],
                   fill=(r, g, b, 250), width=max(1, int(2.4 * scale)))

    else:  # waveform (Audio-style)
        xs = np.linspace(0, vw, int(vw))
        env = (0.5 * (1.0 - np.cos(2.0 * np.pi * xs / max(1, vw)))) ** 1.3
        energy = (0.25 + 0.75 * rms) if state == "listening" else (0.42 + 0.16 * math.sin(frame * 0.14))
        max_amp = (sh - 2 * pad) * 0.56
        k = 2.0 * np.pi / max(1, vw) * 3.2

        # 1. Backing glow & shadow wave
        w_fill = np.sin(xs * k + frame * 0.13) * np.cos(xs * k * 0.5 - frame * 0.07)
        y_fill = cy + w_fill * max_amp * energy * env
        pts_fill = [(pad + int(x), int(y)) for x, y in zip(xs, y_fill)]
        pts_fill += [(pad + int(x), int(cy - (y - cy) * 0.65)) for x, y in reversed(list(zip(xs, y_fill)))]
        if len(pts_fill) > 2:
            d.polygon(pts_fill, fill=(*RGB_BORDER_GLOW, 90))

        # 2. Secondary soft lavender harmonic wave
        w_acc = np.sin(xs * k * 1.3 + frame * 0.19 + 1.2) * env
        y_acc = cy + w_acc * max_amp * energy * 0.75
        pts_acc = [(pad + int(x), int(y)) for x, y in zip(xs, y_acc)]
        for i in range(len(pts_acc) - 1):
            d.line([pts_acc[i], pts_acc[i + 1]], fill=(*RGB_WAVE_ACCENT, 220), width=max(1, int(2.0 * scale)))

        # 3. Primary luminous neon blue wave
        w_main = np.sin(xs * k - frame * 0.15) * env
        y_main = cy + w_main * max_amp * energy * 1.05
        pts_main = [(pad + int(x), int(y)) for x, y in zip(xs, y_main)]
        for i in range(len(pts_main) - 1):
            t = i / max(1, len(pts_main) - 1)
            r = int(RGB_WAVE_HIGHLIGHT[0] + (RGB_WAVE_ACCENT[0] - RGB_WAVE_HIGHLIGHT[0]) * t)
            g = int(RGB_WAVE_HIGHLIGHT[1] + (RGB_WAVE_ACCENT[1] - RGB_WAVE_HIGHLIGHT[1]) * t)
            b = int(RGB_WAVE_HIGHLIGHT[2] + (RGB_WAVE_ACCENT[2] - RGB_WAVE_HIGHLIGHT[2]) * t)
            d.line([pts_main[i], pts_main[i + 1]], fill=(r, g, b, 255), width=max(1, int(2.8 * scale)))

    return img.resize((w, h), Image.Resampling.LANCZOS)


def render_overlay_pill_image(w: int, h: int, style: str, state: str, frame: int, rms: float, text: str) -> Image.Image:
    """Render high-end translucent glass pill with neon gradient glow and crisp typography."""
    scale = 2
    sw, sh = int(w * scale), int(h * scale)
    pad = int(10 * scale)

    # Copy cached precomputed base pill (background, outer glow, and gradient border)
    base = _get_overlay_base(w, h, scale)
    img = base.copy()
    d = ImageDraw.Draw(img)

    cy = sh // 2

    # Visualizer Region
    vx_start = pad + int(18 * scale)
    vx_end = pad + int(112 * scale)
    vw = vx_end - vx_start

    if style == "bars":
        # Capsule Equalizer Bars (Reference Image 2)
        num_bars = 9
        bar_w = int(5.5 * scale)
        gap = (vw - num_bars * bar_w) / max(1, num_bars - 1)
        max_h = (sh - 2 * pad) * 0.72

        for i in range(num_bars):
            bx = int(vx_start + i * (bar_w + gap))
            env = math.sin((i + 1) / (num_bars + 1) * math.pi)
            if state == "listening":
                osc = math.sin(frame * 0.22 + i * 0.75) * 0.3 + 0.7
                h_val = max(int(6 * scale), int(max_h * (0.2 + 0.8 * rms) * env * osc))
            elif state in ("transcribing", "loading", "waking"):
                osc = math.sin(frame * 0.18 - i * 0.55) * 0.35 + 0.65
                h_val = max(int(6 * scale), int(max_h * 0.55 * env * osc))
            else:
                osc = math.sin(frame * 0.08 + i * 0.35) * 0.2 + 0.8
                h_val = max(int(5 * scale), int(max_h * 0.26 * env * osc))

            t = i / max(1, num_bars - 1)
            r = int(RGB_WAVE_HIGHLIGHT[0] + (RGB_WAVE_ACCENT[0] - RGB_WAVE_HIGHLIGHT[0]) * t)
            g = int(RGB_WAVE_HIGHLIGHT[1] + (RGB_WAVE_ACCENT[1] - RGB_WAVE_HIGHLIGHT[1]) * t)
            b = int(RGB_WAVE_HIGHLIGHT[2] + (RGB_WAVE_ACCENT[2] - RGB_WAVE_HIGHLIGHT[2]) * t)

            d.rounded_rectangle((bx, cy - h_val // 2, bx + bar_w, cy + h_val // 2),
                                radius=bar_w // 2, fill=(r, g, b, 255))
            # Subtle center highlight dot
            highlight_h = max(int(3 * scale), int(h_val * 0.35))
            d.rounded_rectangle((bx + 1 * scale, cy - highlight_h // 2, bx + bar_w - 1 * scale, cy + highlight_h // 2),
                                radius=bar_w // 2, fill=(245, 245, 255, 140))

    elif style == "ribbon":
        # Fluid Ribbon
        xs = np.linspace(0, vw, int(vw))
        env = (0.5 * (1.0 - np.cos(2.0 * np.pi * xs / max(1, vw)))) ** 1.2
        energy = (0.2 + 0.8 * rms) if state == "listening" else 0.45
        max_amp = (sh - 2 * pad) * 0.42
        y_top = cy + np.sin(xs * 0.05 + frame * 0.14) * max_amp * energy * env
        y_bot = cy + np.sin(xs * 0.05 + frame * 0.14 + 1.3) * max_amp * energy * env * 0.7
        pts = [(vx_start + int(x), int(yt)) for x, yt in zip(xs, y_top)]
        pts += [(vx_start + int(x), int(yb)) for x, yb in reversed(list(zip(xs, y_bot)))]
        if len(pts) > 2:
            d.polygon(pts, fill=(*RGB_WAVE_SHADOW, 170))
        for i in range(len(xs) - 1):
            t = i / max(1, len(xs) - 1)
            r = int(RGB_WAVE_HIGHLIGHT[0] + (RGB_WAVE_ACCENT[0] - RGB_WAVE_HIGHLIGHT[0]) * t)
            g = int(RGB_WAVE_HIGHLIGHT[1] + (RGB_WAVE_ACCENT[1] - RGB_WAVE_HIGHLIGHT[1]) * t)
            b = int(RGB_WAVE_HIGHLIGHT[2] + (RGB_WAVE_ACCENT[2] - RGB_WAVE_HIGHLIGHT[2]) * t)
            d.line([(vx_start + int(xs[i]), int(y_top[i])), (vx_start + int(xs[i + 1]), int(y_top[i + 1]))],
                   fill=(r, g, b, 255), width=max(1, int(2.6 * scale)))

    else:
        # Waveform (Audio-style) (Reference Image 3)
        xs = np.linspace(0, vw, int(vw))
        env = (0.5 * (1.0 - np.cos(2.0 * np.pi * xs / max(1, vw)))) ** 1.3
        energy = (0.25 + 0.75 * rms) if state == "listening" else (0.42 + 0.16 * math.sin(frame * 0.14))
        max_amp = (sh - 2 * pad) * 0.44
        k = 2.0 * np.pi / max(1, vw) * 2.8

        # 1. Backing translucent fill & shadow wave
        w_fill = np.sin(xs * k + frame * 0.14) * np.cos(xs * k * 0.5 - frame * 0.08)
        y_fill = cy + w_fill * max_amp * energy * env
        pts_fill = [(vx_start + int(x), int(y)) for x, y in zip(xs, y_fill)]
        pts_fill += [(vx_start + int(x), int(cy - (y - cy) * 0.65)) for x, y in reversed(list(zip(xs, y_fill)))]
        if len(pts_fill) > 2:
            d.polygon(pts_fill, fill=(*RGB_BORDER_GLOW, 85))

        # 2. Secondary soft lavender harmonic wave
        w_acc = np.sin(xs * k * 1.35 + frame * 0.2 + 1.2) * env
        y_acc = cy + w_acc * max_amp * energy * 0.75
        pts_acc = [(vx_start + int(x), int(y)) for x, y in zip(xs, y_acc)]
        for i in range(len(pts_acc) - 1):
            d.line([pts_acc[i], pts_acc[i + 1]], fill=(*RGB_WAVE_ACCENT, 220), width=max(1, int(2.0 * scale)))

        # 3. Primary luminous neon blue wave
        w_main = np.sin(xs * k - frame * 0.16) * env
        y_main = cy + w_main * max_amp * energy * 1.05
        pts_main = [(vx_start + int(x), int(y)) for x, y in zip(xs, y_main)]
        for i in range(len(pts_main) - 1):
            t = i / max(1, len(pts_main) - 1)
            r = int(RGB_WAVE_HIGHLIGHT[0] + (RGB_WAVE_ACCENT[0] - RGB_WAVE_HIGHLIGHT[0]) * t)
            g = int(RGB_WAVE_HIGHLIGHT[1] + (RGB_WAVE_ACCENT[1] - RGB_WAVE_HIGHLIGHT[1]) * t)
            b = int(RGB_WAVE_HIGHLIGHT[2] + (RGB_WAVE_ACCENT[2] - RGB_WAVE_HIGHLIGHT[2]) * t)
            d.line([pts_main[i], pts_main[i + 1]], fill=(r, g, b, 255), width=max(1, int(3.0 * scale)))

    # Divider: Vertical hairline in Dark Purple (#332B5D)
    div_x = vx_end + int(14 * scale)
    div_h = int(14 * scale)
    d.line((div_x, cy - div_h, div_x, cy + div_h), fill=(*RGB_WAVE_SHADOW, 230), width=max(1, int(1.5 * scale)))

    # Typography: Cool White (#EAEAF0) Segoe UI
    font = get_system_font(int(13.5 * scale), bold=False)
    tx = div_x + int(16 * scale)
    bbox = font.getbbox(text)
    th = bbox[3] - bbox[1]
    d.text((tx, cy - th // 2 - int(2 * scale)), text, font=font, fill=(*RGB_TEXT_MAIN, 255))

    return img.resize((w, h), Image.Resampling.LANCZOS)


# ============================================================================
# Floating Overlay Window (High-DPI Translucent Indicator Pill)
# ============================================================================
KEY_COLOR = "#010204"


class FloatingOverlay:
    """Floating rounded status pill with transparent background and smooth animations."""

    def __init__(self, root, width: int = 420, height: int = 70):
        self.root = root
        self.width = width
        self.height = height
        self.visible = False
        self.x = 0
        self.y = 0

        self.top = tk.Toplevel(root)
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", True)
        try:
            self.top.wm_attributes("-toolwindow", True)
        except Exception:
            pass
        self.top.configure(bg=KEY_COLOR)
        try:
            self.top.wm_attributes("-transparentcolor", KEY_COLOR)
        except Exception:
            pass

        self.canvas = tk.Canvas(
            self.top,
            width=self.width,
            height=self.height,
            bg=KEY_COLOR,
            bd=0,
            highlightthickness=0,
            relief="flat",
        )
        self.canvas.pack(fill="both", expand=True)
        self.canvas_img_ref = None
        self.canvas_item = None

        # Prevent floating overlay from stealing focus from active application
        try:
            self.top.update_idletasks()
            user32 = ctypes.windll.user32
            child_hwnd = self.top.winfo_id()
            self.root_hwnd = user32.GetAncestor(child_hwnd, 2)
            GWL_EXSTYLE = -20
            WS_EX_NOACTIVATE = 0x08000000
            style = user32.GetWindowLongW(self.root_hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(self.root_hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE)
        except Exception:
            self.root_hwnd = None

        self.top.withdraw()

    def update_position(self):
        try:
            class RECT(ctypes.Structure):
                _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG), ("right", wintypes.LONG), ("bottom", wintypes.LONG)]
            rc = RECT()
            SPI_GETWORKAREA = 0x0030
            if ctypes.windll.user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rc), 0):
                sw = rc.right - rc.left
                self.x = rc.left + (sw - self.width) // 2
                self.y = rc.bottom - self.height - 48
            else:
                sw = self.top.winfo_screenwidth()
                sh = self.top.winfo_screenheight()
                self.x = (sw - self.width) // 2
                self.y = sh - self.height - 75
        except Exception:
            sw = self.top.winfo_screenwidth()
            sh = self.top.winfo_screenheight()
            self.x = (sw - self.width) // 2
            self.y = sh - self.height - 75

    def is_visible(self) -> bool:
        return self.visible

    def show(self, text: str, state: str, style: str, frame: int, rms: float):
        self.visible = True
        self.update_position()
        self.top.geometry(f"{self.width}x{self.height}+{self.x}+{self.y}")
        self.update_frame(text, state, style, frame, rms)
        self.top.deiconify()
        self.top.lift()
        self.top.attributes("-topmost", True)

    def update_frame(self, text: str, state: str, style: str, frame: int, rms: float):
        if not self.visible:
            return
        pill = render_overlay_pill_image(self.width, self.height, style, state, frame, rms, text)
        bg = Image.new("RGB", (self.width, self.height), (1, 2, 4))
        bg.paste(pill, (0, 0), pill)
        photo = ImageTk.PhotoImage(bg)
        self.canvas_img_ref = photo

        if self.canvas_item is None:
            self.canvas_item = self.canvas.create_image(self.width // 2, self.height // 2, image=photo)
        else:
            self.canvas.itemconfig(self.canvas_item, image=photo)

    def hide(self):
        if not self.visible:
            return
        self.visible = False
        try:
            self.top.withdraw()
        except Exception:
            pass

    def destroy(self):
        try:
            self.top.destroy()
        except Exception:
            pass


# ============================================================================
# Main RorishperFlow Application
# ============================================================================
class RorishperFlow:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("520x790")
        self.root.minsize(480, 740)
        self.root.configure(fg_color=COLOR_MAIN_BG)
        self.settings = load_settings()
        self.hotkey = set(self.settings["hotkey"])
        self.held_keys = set()
        self.capture_keys = set()
        self.capture_down = set()
        self.capturing = False
        self.recording = False
        self.sleeping = False
        self.was_sleeping = False
        self.suppress_shortcut = False
        self._stop_requested = False
        self.audio_parts = []
        self.events = queue.Queue()
        self.model = None
        self.device_used = None
        self.model_loading = False
        self.cublas_warmed_up = False
        self.model_paths = {}
        self.transcribing = False
        self.pending_audios = []
        self.model_lock = threading.Lock()
        self.last_activity = time.monotonic()
        self.runtime_state = "loading"
        self.anim_frame = 0
        self.latest_rms = 0.0
        self.selected_model = self.settings["model"]
        self.model_var = ctk.StringVar(value=self.settings["model"])
        self.animation_style_var = ctk.StringVar(value=self.settings.get("animation_style", "Waveform (Audio-style)"))
        self.status_var = ctk.StringVar(value="Starting up…")
        self.paste_var = ctk.BooleanVar(value=self.settings.get("paste", True))
        self.idle_unload_var = ctk.BooleanVar(value=self.settings.get("idle_unload", True))
        self.overlay_var = ctk.BooleanVar(value=self.settings.get("show_overlay", True))

        # Floating overlay state
        self.overlay_text = "Listening — release to finish"
        self.overlay_mode = "listening"

        # Start pynput keyboard listener (for hotkey capture in UI)
        self.listener = keyboard.Listener(on_press=self.on_key_down, on_release=self.on_key_up)
        self.listener.start()

        # Start dedicated background hardware hotkey poller via GetAsyncKeyState
        self.poller_thread = threading.Thread(target=self._poll_hotkeys_loop, daemon=True)
        self.poller_thread.start()

        self._init_overlay()
        self._build_ui()
        self._set_window_icon()
        self._start_tray()
        self.root.after(180, self._round_main_window)
        self.root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        self.root.after(25, self.process_events)
        self.root.after(10_000, self.check_idle)
        self.request_model_load()

    def _poll_hotkeys_loop(self):
        """Dedicated background poller using Windows GetAsyncKeyState.
        Ensures global hotkey detection works 100% reliably even after hours of background sleep."""
        while not self._stop_requested:
            try:
                time.sleep(0.02)
                if self.capturing or self.suppress_shortcut or not self.hotkey:
                    continue
                all_down = all(is_key_down(k) for k in self.hotkey)

                if all_down and not self.recording:
                    self.start_recording()
                elif not all_down and self.recording:
                    self.stop_recording()
            except Exception:
                time.sleep(0.05)

    def _init_overlay(self):
        """Initialize high-end translucent glass pill overlay."""
        self.floating_overlay = FloatingOverlay(self.root, width=420, height=70)

    def preview_indicator_pill(self):
        """Display the floating indicator pill for 3.5 seconds to preview it."""
        self._show_overlay("Listening — release to finish", mode="listening")
        self.root.after(3500, self._hide_overlay)

    def _show_overlay(self, text, mode="listening"):
        if not self.overlay_var.get():
            return
        self.overlay_mode = mode
        self.overlay_text = text
        if hasattr(self, "floating_overlay"):
            self.floating_overlay.show(
                text=text,
                state=mode,
                style=self.get_animation_style_key(),
                frame=self.anim_frame,
                rms=self.latest_rms,
            )

    def _hide_overlay(self):
        if hasattr(self, "floating_overlay"):
            self.floating_overlay.hide()

    def get_animation_style_key(self) -> str:
        val = self.animation_style_var.get().lower()
        if "bar" in val or "capsule" in val:
            return "bars"
        elif "ribbon" in val:
            return "ribbon"
        return "waveform"

    def _draw_status_icon(self, color):
        if not hasattr(self, "status_dot") or not self.status_dot.winfo_exists():
            return
        self.status_dot.delete("all")
        self.status_dot.create_oval(1, 1, 14, 14, outline=color, width=1.5)
        self.status_dot.create_oval(4, 4, 11, 11, fill=color, outline="")

    @staticmethod
    def _draw_gpu_icon(canvas):
        canvas.create_rectangle(3, 4, 16, 15, outline=COLOR_TEXT_MUTED, width=1.3)
        canvas.create_rectangle(6, 7, 13, 12, outline=COLOR_WAVE_ACCENT, width=1.2)
        for x in (5, 9, 13):
            canvas.create_line(x, 2, x, 4, fill=COLOR_TEXT_MUTED, width=1)
            canvas.create_line(x, 15, x, 18, fill=COLOR_TEXT_MUTED, width=1)
        canvas.create_line(16, 9, 19, 9, fill=COLOR_TEXT_MUTED, width=1)

    def _round_main_window(self):
        """Use Windows 11 rounded native frame with dark mode titlebar."""
        if os.name != "nt":
            return
        try:
            self.root.update_idletasks()
            hwnd = self.root.winfo_id()
            corner = ctypes.c_int(2)  # DWMWCP_ROUND
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(corner), ctypes.sizeof(corner))
            # Set dark mode title bar
            dark_mode = ctypes.c_int(1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark_mode), ctypes.sizeof(dark_mode))
        except Exception:
            pass

    def label(self, parent, text="", **kwargs):
        return ctk.CTkLabel(parent, text=text, **kwargs)

    def _build_ui(self):
        # --------------------------------------------------------------------
        # Main Scrollable Container (with styled scrollbar)
        # --------------------------------------------------------------------
        self.main_scroll = ctk.CTkScrollableFrame(
            self.root,
            fg_color=COLOR_MAIN_BG,
            scrollbar_fg_color=COLOR_COMP_BG,
            scrollbar_button_color=COLOR_WAVE_SHADOW,
            scrollbar_button_hover_color=COLOR_BORDER_GLOW,
            corner_radius=0,
        )
        self.main_scroll.pack(fill="both", expand=True)
        self.main_scroll.grid_columnconfigure(0, weight=1)

        # --------------------------------------------------------------------
        # 1. Header Row
        # --------------------------------------------------------------------
        header = ctk.CTkFrame(self.main_scroll, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=26, pady=(22, 12))

        mark_path = APP_DIR / "icons" / "app_mark.png"
        if not mark_path.exists():
            mark_path = APP_DIR / "icons" / "logo_clean.png"
        if not mark_path.exists():
            mark_path = APP_DIR / "icons" / "logo.png"

        logo_loaded = False
        if mark_path.exists():
            try:
                logo_pil = Image.open(mark_path).convert("RGBA")
                self.logo_image = ctk.CTkImage(light_image=logo_pil, dark_image=logo_pil, size=(42, 42))
                mark_label = ctk.CTkLabel(header, image=self.logo_image, text="", width=42, height=42)
                mark_label.pack(side="left")
                logo_loaded = True
            except Exception:
                logo_loaded = False

        if not logo_loaded:
            mark = ctk.CTkFrame(header, width=42, height=42, corner_radius=13, fg_color=COLOR_BORDER_GLOW)
            mark.pack(side="left")
            mark.pack_propagate(False)
            self.label(mark, "R", font=ctk.CTkFont(size=23, weight="bold"), text_color=COLOR_TEXT_MAIN).place(
                relx=0.5, rely=0.47, anchor="center"
            )

        title = ctk.CTkFrame(header, fg_color="transparent")
        title.pack(side="left", padx=12)
        self.label(title, APP_NAME, font=ctk.CTkFont(size=20, weight="bold"), text_color=COLOR_TEXT_MAIN).pack(anchor="w")
        self.label(title, "PRIVATE, LOCAL VOICE TYPING", font=ctk.CTkFont(size=9, weight="bold"), text_color=COLOR_TEXT_MUTED).pack(
            anchor="w", pady=(2, 0)
        )

        site_button = ctk.CTkButton(
            header,
            text="rohandas.is-a.dev",
            height=28,
            corner_radius=8,
            fg_color=COLOR_BUTTON_BG,
            hover_color=COLOR_BUTTON_HOVER,
            border_width=1,
            border_color=COLOR_WAVE_SHADOW,
            text_color=COLOR_WAVE_ACCENT,
            font=ctk.CTkFont(size=11, weight="bold"),
            command=lambda: webbrowser.open("https://rohandas.is-a.dev/"),
            cursor="hand2",
        )
        site_button.pack(side="right")

        # --------------------------------------------------------------------
        # 2. Hero Card (Speech Trigger & Dynamic Visualizer)
        # --------------------------------------------------------------------
        self.hero = ctk.CTkFrame(
            self.main_scroll,
            fg_color=COLOR_COMP_BG,
            corner_radius=22,
            border_width=1.5,
            border_color=COLOR_WAVE_SHADOW,
        )
        self.hero.grid(row=1, column=0, sticky="ew", padx=22, pady=6)
        self.hero.grid_columnconfigure(0, weight=1)

        self.visualizer_frame = ctk.CTkFrame(
            self.hero,
            width=254,
            height=60,
            corner_radius=20,
            fg_color=COLOR_HERO_BOX,
            border_width=1,
            border_color=COLOR_WAVE_SHADOW,
        )
        self.visualizer_frame.grid(row=0, column=0, pady=(17, 7))
        self.visualizer_frame.grid_propagate(False)

        self.wave_canvas = tk.Canvas(
            self.visualizer_frame,
            width=238,
            height=48,
            bg=COLOR_HERO_BOX,
            bd=0,
            highlightthickness=0,
            relief="flat",
        )
        self.wave_canvas.place(relx=0.5, rely=0.5, anchor="center")
        self.wave_canvas_img_ref = None
        self._update_hero_visualizer()

        self.label(self.hero, "HOLD TO SPEAK", font=ctk.CTkFont(size=10, weight="bold"), text_color=COLOR_TEXT_MUTED).grid(
            row=1, column=0
        )
        self.label(self.hero, "Release to transcribe", font=ctk.CTkFont(size=18, weight="bold"), text_color=COLOR_TEXT_MAIN).grid(
            row=2, column=0, pady=(4, 3)
        )

        self.shortcut_pill = ctk.CTkButton(
            self.hero,
            text=self.shortcut_text(),
            height=32,
            width=132,
            corner_radius=10,
            fg_color=COLOR_BUTTON_BG,
            hover_color=COLOR_BUTTON_HOVER,
            border_width=1,
            border_color=COLOR_WAVE_SHADOW,
            text_color=COLOR_WAVE_ACCENT,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self.capture_hotkey,
        )
        self.shortcut_pill.grid(row=3, column=0, pady=(8, 17))

        self.change_shortcut_button = ctk.CTkButton(
            self.hero,
            text="Change shortcut",
            height=28,
            width=132,
            corner_radius=8,
            fg_color="transparent",
            hover_color=COLOR_BUTTON_BG,
            text_color=COLOR_WAVE_ACCENT,
            font=ctk.CTkFont(size=11, weight="bold"),
            command=self.capture_hotkey,
        )
        self.change_shortcut_button.grid(row=4, column=0, pady=(0, 12))

        # --------------------------------------------------------------------
        # 3. Status Row
        # --------------------------------------------------------------------
        status_row = ctk.CTkFrame(self.main_scroll, fg_color="transparent")
        status_row.grid(row=2, column=0, sticky="ew", padx=26, pady=(12, 5))

        self.status_dot = tk.Canvas(status_row, width=15, height=15, bg=COLOR_MAIN_BG, bd=0, highlightthickness=0)
        self.status_dot.pack(side="left", padx=(0, 8))
        self._draw_status_icon("#F59E0B")

        self.status_label = self.label(
            status_row, textvariable=self.status_var, text_color=COLOR_TEXT_MAIN, font=ctk.CTkFont(size=12)
        )
        self.status_label.pack(side="left")

        # --------------------------------------------------------------------
        # 4. Transcript Box
        # --------------------------------------------------------------------
        self.transcript = ctk.CTkTextbox(
            self.main_scroll,
            height=120,
            corner_radius=16,
            fg_color=COLOR_COMP_BG,
            border_width=1,
            border_color=COLOR_WAVE_SHADOW,
            text_color=COLOR_TEXT_MAIN,
            font=ctk.CTkFont(size=13),
            wrap="word",
        )
        self.transcript.grid(row=3, column=0, sticky="ew", padx=22, pady=(6, 4))
        self.transcript.insert("1.0", "Your transcription will appear here…")
        self.transcript.configure(state="disabled")

        # --------------------------------------------------------------------
        # 5. Preferences Card
        # --------------------------------------------------------------------
        settings = ctk.CTkFrame(
            self.main_scroll,
            fg_color=COLOR_COMP_BG,
            corner_radius=18,
            border_width=1,
            border_color=COLOR_WAVE_SHADOW,
        )
        settings.grid(row=4, column=0, sticky="ew", padx=22, pady=(8, 8))
        settings.grid_columnconfigure(1, weight=1)

        self.label(settings, "PREFERENCES", font=ctk.CTkFont(size=10, weight="bold"), text_color=COLOR_TEXT_MUTED).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=16, pady=(12, 4)
        )

        # Model row
        self.label(settings, "Model", text_color=COLOR_TEXT_MAIN, font=ctk.CTkFont(size=12)).grid(
            row=1, column=0, sticky="w", padx=16, pady=6
        )
        self.model_menu = ctk.CTkOptionMenu(
            settings,
            variable=self.model_var,
            values=["tiny", "base", "small", "medium", "large-v3-turbo", "large-v3"],
            width=140,
            height=30,
            fg_color=COLOR_BUTTON_BG,
            button_color=COLOR_WAVE_SHADOW,
            button_hover_color=COLOR_BORDER_GLOW,
            text_color=COLOR_TEXT_MAIN,
            command=self.on_model_selected,
        )
        self.model_menu.grid(row=1, column=1, sticky="e", padx=16, pady=6)

        # Animation Style Setting
        self.label(settings, "Animation Style", text_color=COLOR_TEXT_MAIN, font=ctk.CTkFont(size=12)).grid(
            row=2, column=0, sticky="w", padx=16, pady=6
        )
        self.anim_style_menu = ctk.CTkOptionMenu(
            settings,
            variable=self.animation_style_var,
            values=ANIMATION_STYLES,
            width=180,
            height=30,
            fg_color=COLOR_BUTTON_BG,
            button_color=COLOR_WAVE_SHADOW,
            button_hover_color=COLOR_BORDER_GLOW,
            text_color=COLOR_TEXT_MAIN,
            command=self.on_animation_style_selected,
        )
        self.anim_style_menu.grid(row=2, column=1, sticky="e", padx=16, pady=6)

        # Language row
        self.label(settings, "Language", text_color=COLOR_TEXT_MAIN, font=ctk.CTkFont(size=12)).grid(
            row=3, column=0, sticky="w", padx=16, pady=6
        )
        self.label(settings, "Auto · English + Hindi", text_color=COLOR_WAVE_ACCENT, font=ctk.CTkFont(size=12, weight="bold")).grid(
            row=3, column=1, sticky="e", padx=16, pady=6
        )

        # Switches
        ctk.CTkSwitch(
            settings,
            text="Paste into the active app",
            variable=self.paste_var,
            command=self.save_settings,
            progress_color=COLOR_BORDER_GLOW,
            button_color=COLOR_TEXT_MAIN,
            button_hover_color=COLOR_WAVE_ACCENT,
            text_color=COLOR_TEXT_MAIN,
            font=ctk.CTkFont(size=12),
        ).grid(row=4, column=0, columnspan=2, sticky="w", padx=16, pady=(6, 9))

        ctk.CTkSwitch(
            settings,
            text="Release GPU memory after 5 minutes idle",
            variable=self.idle_unload_var,
            command=self.save_settings,
            progress_color=COLOR_BORDER_GLOW,
            button_color=COLOR_TEXT_MAIN,
            button_hover_color=COLOR_WAVE_ACCENT,
            text_color=COLOR_TEXT_MAIN,
            font=ctk.CTkFont(size=12),
        ).grid(row=5, column=0, columnspan=2, sticky="w", padx=16, pady=(0, 9))

        overlay_row = ctk.CTkFrame(settings, fg_color="transparent")
        overlay_row.grid(row=6, column=0, columnspan=2, sticky="ew", padx=16, pady=(0, 12))
        overlay_row.grid_columnconfigure(0, weight=1)

        ctk.CTkSwitch(
            overlay_row,
            text="Show floating indicator pill when speaking",
            variable=self.overlay_var,
            command=self.save_settings,
            progress_color=COLOR_BORDER_GLOW,
            button_color=COLOR_TEXT_MAIN,
            button_hover_color=COLOR_WAVE_ACCENT,
            text_color=COLOR_TEXT_MAIN,
            font=ctk.CTkFont(size=12),
        ).pack(side="left")

        ctk.CTkButton(
            overlay_row,
            text="Preview Pill",
            width=90,
            height=28,
            corner_radius=8,
            fg_color=COLOR_BUTTON_BG,
            hover_color=COLOR_BUTTON_HOVER,
            border_width=1,
            border_color=COLOR_WAVE_SHADOW,
            text_color=COLOR_WAVE_ACCENT,
            font=ctk.CTkFont(size=11, weight="bold"),
            command=self.preview_indicator_pill,
        ).pack(side="right")

        # --------------------------------------------------------------------
        # 6. Footer & Credits Row
        # --------------------------------------------------------------------
        footer = ctk.CTkFrame(self.main_scroll, fg_color="transparent")
        footer.grid(row=5, column=0, sticky="ew", padx=26, pady=(4, 10))

        gpu_mark = tk.Canvas(footer, width=20, height=20, bg=COLOR_MAIN_BG, bd=0, highlightthickness=0)
        gpu_mark.pack(side="left", padx=(0, 7))
        self._draw_gpu_icon(gpu_mark)

        self.label(footer, "NVIDIA GPU · releases memory after 5 min idle", text_color=COLOR_TEXT_MUTED, font=ctk.CTkFont(size=11)).pack(
            side="left"
        )

        self.reload_button = ctk.CTkButton(
            footer,
            text="Reload model",
            width=105,
            height=30,
            corner_radius=9,
            fg_color=COLOR_BUTTON_BG,
            hover_color=COLOR_BUTTON_HOVER,
            border_width=1,
            border_color=COLOR_WAVE_SHADOW,
            text_color=COLOR_TEXT_MAIN,
            font=ctk.CTkFont(size=11),
            command=self.reload_model,
        )
        self.reload_button.pack(side="right")

        credits_row = ctk.CTkFrame(self.main_scroll, fg_color="transparent")
        credits_row.grid(row=6, column=0, sticky="ew", padx=26, pady=(0, 20))

        ctk.CTkButton(
            credits_row,
            text="Created by Rohan Das · https://rohandas.is-a.dev/",
            height=24,
            corner_radius=6,
            fg_color="transparent",
            hover_color=COLOR_BUTTON_BG,
            text_color=COLOR_TEXT_MUTED,
            font=ctk.CTkFont(size=11),
            command=lambda: webbrowser.open("https://rohandas.is-a.dev/"),
            cursor="hand2",
        ).pack(side="left")

    def _update_hero_visualizer(self):
        """Update hero visualizer on wave_canvas."""
        if not hasattr(self, "wave_canvas") or not self.wave_canvas.winfo_exists():
            return
        style_key = self.get_animation_style_key()
        img = render_hero_visualizer_image(
            238, 48, style_key, self.runtime_state, self.anim_frame, self.latest_rms
        )
        photo = ImageTk.PhotoImage(img)
        self.wave_canvas_img_ref = photo
        self.wave_canvas.delete("all")
        self.wave_canvas.create_image(119, 24, image=photo)

    def on_animation_style_selected(self, _choice=None):
        self.save_settings()
        self._update_hero_visualizer()
        if hasattr(self, "floating_overlay") and self.floating_overlay.is_visible():
            self.floating_overlay.update_frame(
                self.overlay_text,
                self.overlay_mode,
                self.get_animation_style_key(),
                self.anim_frame,
                self.latest_rms,
            )

    def make_icon_image(self, state, frame=0, size=64):
        """Render high-contrast, visually prominent tray icon with active animations and clear indicators."""
        s = size / 64.0
        logo_path = APP_DIR / "icons" / "logo_clean.png"
        if not logo_path.exists():
            logo_path = APP_DIR / "icons" / "logo.png"

        image = None
        if logo_path.exists():
            try:
                base_img = Image.open(logo_path).convert("RGBA")
                image = base_img.resize((size, size), Image.Resampling.LANCZOS)
            except Exception:
                image = None

        if image is None:
            image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            draw = ImageDraw.Draw(image)
            palettes = {
                "ready": {
                    "bg": COLOR_MAIN_BG, "border": COLOR_BORDER_GLOW, "mic": COLOR_TEXT_MAIN,
                    "cradle": COLOR_WAVE_HIGHLIGHT, "badge": "#10B981", "badge_hi": "#A7F3D0"
                },
                "listening": {
                    "bg": COLOR_MAIN_BG, "border": COLOR_WAVE_HIGHLIGHT, "mic": COLOR_TEXT_MAIN,
                    "cradle": COLOR_WAVE_ACCENT, "badge": COLOR_WAVE_HIGHLIGHT, "badge_hi": COLOR_WAVE_ACCENT
                },
                "transcribing": {
                    "bg": COLOR_MAIN_BG, "border": COLOR_WAVE_ACCENT, "mic": COLOR_TEXT_MAIN,
                    "cradle": COLOR_BORDER_GLOW, "badge": COLOR_WAVE_ACCENT, "badge_hi": COLOR_TEXT_MAIN
                },
                "loading": {
                    "bg": COLOR_MAIN_BG, "border": "#F59E0B", "mic": COLOR_TEXT_MAIN,
                    "cradle": "#FBBF24", "badge": "#F59E0B", "badge_hi": "#FEF3C7"
                },
                "sleeping": {
                    "bg": COLOR_MAIN_BG, "border": COLOR_WAVE_SHADOW, "mic": COLOR_TEXT_MUTED,
                    "cradle": "#474E68", "badge": COLOR_BORDER_GLOW, "badge_hi": COLOR_WAVE_ACCENT
                },
                "error": {
                    "bg": COLOR_MAIN_BG, "border": "#EF4444", "mic": COLOR_TEXT_MAIN,
                    "cradle": "#F87171", "badge": "#EF4444", "badge_hi": "#FEE2E2"
                },
            }
            p = palettes.get(state, palettes["ready"])
            bg_rgb = tuple(int(p["bg"][i:i+2], 16) for i in (1, 3, 5))
            accent_rgb = tuple(int(p["border"][i:i+2], 16) for i in (1, 3, 5))
            gradient = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            gd = ImageDraw.Draw(gradient)
            for y in range(size):
                t = 0.24 * (1 - y / max(1, size - 1))
                rgb = tuple(round(a + (b - a) * t) for a, b in zip(bg_rgb, accent_rgb))
                gd.line((0, y, size, y), fill=(*rgb, 255))
            mask = Image.new("L", (size, size), 0)
            ImageDraw.Draw(mask).rounded_rectangle((2*s, 2*s, 62*s, 62*s), radius=16*s, fill=255)
            image.paste(gradient, (0, 0), mask)
            draw = ImageDraw.Draw(image)
            border_w = max(2, int(3.5 * s))
            draw.rounded_rectangle((2*s, 2*s, 62*s, 62*s), radius=16*s, outline=p["border"], width=border_w)
            draw.rounded_rectangle((25*s, 11*s, 39*s, 34*s), radius=7*s, fill=p["mic"])
            draw.arc((17*s, 19*s, 47*s, 41*s), start=0, end=180, fill=p["cradle"], width=max(2, int(4*s)))
            draw.line([(32*s, 41*s), (32*s, 49*s)], fill=p["cradle"], width=max(2, int(4*s)))
            draw.line([(24*s, 49*s), (40*s, 49*s)], fill=p["cradle"], width=max(2, int(4*s)))

        draw = ImageDraw.Draw(image)

        # Animated visual effects
        if state == "listening":
            pulse_stage = frame % 3
            wave_color1 = COLOR_WAVE_HIGHLIGHT
            wave_color2 = COLOR_WAVE_ACCENT if pulse_stage == 2 else COLOR_BORDER_GLOW
            draw.arc((10*s, 16*s, 22*s, 36*s), start=105, end=255, fill=wave_color1, width=max(2, int(3*s)))
            draw.arc((42*s, 16*s, 54*s, 36*s), start=285, end=75, fill=wave_color1, width=max(2, int(3*s)))
            if pulse_stage >= 1:
                draw.arc((4*s, 11*s, 18*s, 41*s), start=105, end=255, fill=wave_color2, width=max(2, int(3*s)))
                draw.arc((46*s, 11*s, 60*s, 41*s), start=285, end=75, fill=wave_color2, width=max(2, int(3*s)))

        elif state == "transcribing":
            deg = (frame * 40) % 360
            rad = math.radians(deg)
            cx, cy = 32*s, 32*s
            orb_r = 25*s
            x1 = cx + orb_r * math.cos(rad)
            y1 = cy + orb_r * math.sin(rad)
            draw.ellipse((x1-4.5*s, y1-4.5*s, x1+4.5*s, y1+4.5*s), fill=COLOR_WAVE_HIGHLIGHT, outline="#ffffff", width=max(1, int(1.5*s)))
            x2 = cx + orb_r * math.cos(rad + math.pi)
            y2 = cy + orb_r * math.sin(rad + math.pi)
            draw.ellipse((x2-4*s, y2-4*s, x2+4*s, y2+4*s), fill=COLOR_WAVE_ACCENT, outline="#ffffff", width=max(1, int(1.5*s)))

        elif state == "loading":
            start_ang = (frame * 45) % 360
            draw.arc((5*s, 5*s, 59*s, 59*s), start=start_ang, end=start_ang+110, fill="#FBBF24", width=max(2, int(4*s)))

        badge_palettes = {
            "ready": ("#10B981", "#A7F3D0"),
            "listening": (COLOR_WAVE_HIGHLIGHT, COLOR_WAVE_ACCENT),
            "transcribing": (COLOR_WAVE_ACCENT, "#FFFFFF"),
            "loading": ("#F59E0B", "#FEF3C7"),
            "sleeping": (COLOR_BORDER_GLOW, COLOR_WAVE_ACCENT),
            "error": ("#EF4444", "#FEE2E2"),
        }
        b_color, b_hi = badge_palettes.get(state, badge_palettes["ready"])
        bx, by = 50*s, 50*s
        badge_r = 10.5*s
        if state == "listening":
            badge_r += (1.5 * s if (frame % 2 == 0) else 0)

        if state == "sleeping":
            draw.ellipse((bx-badge_r-2*s, by-badge_r-2*s, bx+badge_r+2*s, by+badge_r+2*s), fill=COLOR_MAIN_BG)
            draw.ellipse((bx-badge_r, by-badge_r, bx+badge_r, by+badge_r), fill="#1E2235", outline="#475569", width=max(1, int(2*s)))
            draw.arc((bx-5*s, by-5*s, bx+5*s, by+5*s), start=50, end=250, fill=COLOR_WAVE_ACCENT, width=max(2, int(2.5*s)))
        else:
            draw.ellipse((bx-badge_r-2.5*s, by-badge_r-2.5*s, bx+badge_r+2.5*s, by+badge_r+2.5*s), fill=COLOR_MAIN_BG)
            draw.ellipse((bx-badge_r, by-badge_r, bx+badge_r, by+badge_r), fill=b_color, outline="#ffffff", width=max(1, int(1.5*s)))
            draw.ellipse((bx-3*s, by-3*s, bx+3*s, by+3*s), fill=b_hi)

        return image

    def _set_window_icon(self):
        """Set high-resolution window and taskbar icons."""
        ico_path = APP_DIR / "icons" / "app.ico"
        png_path = APP_DIR / "icons" / "logo_clean.png"
        if not png_path.exists():
            png_path = APP_DIR / "icons" / "logo.png"

        # 1. Tkinter iconphoto (PNG alpha transparency)
        try:
            if png_path.exists():
                img = Image.open(png_path).resize((64, 64), Image.Resampling.LANCZOS)
                self.window_icon = ImageTk.PhotoImage(img)
                self.root.iconphoto(True, self.window_icon)
            else:
                buffer = io.BytesIO()
                self.make_icon_image("ready", 0).save(buffer, format="PNG")
                encoded = base64.b64encode(buffer.getvalue())
                self.window_icon = tk.PhotoImage(data=encoded)
                self.root.iconphoto(True, self.window_icon)
        except Exception:
            pass

        # 2. Native Windows multi-resolution .ico
        if ico_path.exists():
            try:
                self.root.iconbitmap(str(ico_path))
            except Exception:
                pass

        # 3. Direct Win32 WM_SETICON for immediate taskbar update
        self._apply_win32_icon()
        self.root.after(80, self._apply_win32_icon)

    def _apply_win32_icon(self):
        if os.name != "nt":
            return
        ico_path = APP_DIR / "icons" / "app.ico"
        if not ico_path.exists():
            return
        try:
            self.root.update_idletasks()
            hwnd = self.root.winfo_id()
            IMAGE_ICON = 1
            LR_LOADFROMFILE = 0x00000010
            WM_SETICON = 0x0080
            ICON_SMALL = 0
            ICON_BIG = 1

            hicon_small = ctypes.windll.user32.LoadImageW(
                None, str(ico_path), IMAGE_ICON, 16, 16, LR_LOADFROMFILE
            )
            hicon_big = ctypes.windll.user32.LoadImageW(
                None, str(ico_path), IMAGE_ICON, 32, 32, LR_LOADFROMFILE
            )
            if hicon_small:
                ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hicon_small)
            if hicon_big:
                ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, hicon_big)
        except Exception:
            pass

    def _start_tray(self):
        menu = pystray.Menu(
            pystray.MenuItem("Open Rorishper", self._tray_show, default=True),
            pystray.MenuItem(lambda item: self.tray_status_text(), self._tray_noop, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._tray_quit),
        )
        self.tray = pystray.Icon("rorishper", self.make_icon_image("loading", 0), f"Rorishper · {self.tray_status_text()}", menu)
        threading.Thread(target=self._run_tray_safe, daemon=True).start()
        self.root.after(33, self.animate_waveforms)
        self.root.after(180, self.animate_tray)

    def _run_tray_safe(self):
        try:
            self.tray.run()
        except Exception:
            pass

    def tray_status_text(self):
        return {
            "ready": "Ready · GPU active",
            "listening": "Listening… (Recording)",
            "transcribing": "Transcribing on GPU…",
            "loading": "Waking GPU model…",
            "sleeping": "Sleeping · GPU memory released",
            "error": "GPU error",
        }.get(self.runtime_state, "Rorishper")

    def set_runtime_state(self, state):
        self.runtime_state = state
        self.anim_frame = 0
        if state == "listening":
            self.hero.configure(border_color=COLOR_BORDER_GLOW)
        elif state == "transcribing":
            self.hero.configure(border_color=COLOR_WAVE_ACCENT)
        else:
            self.hero.configure(border_color=COLOR_WAVE_SHADOW)

        self._update_hero_visualizer()
        if hasattr(self, "tray"):
            try:
                self.tray.icon = self.make_icon_image(state, 0)
                self.tray.title = f"Rorishper · {self.tray_status_text()}"
                self.tray.update_menu()
            except Exception:
                pass

    def animate_tray(self):
        if hasattr(self, "tray"):
            if self.runtime_state in ("listening", "transcribing", "loading"):
                try:
                    self.tray.icon = self.make_icon_image(self.runtime_state, self.anim_frame)
                except Exception:
                    pass
        self.root.after(180, self.animate_tray)

    def animate_waveforms(self):
        self.anim_frame = (self.anim_frame + 1) % 360
        self._update_hero_visualizer()
        if hasattr(self, "floating_overlay") and self.floating_overlay.is_visible():
            self.floating_overlay.update_frame(
                self.overlay_text,
                self.overlay_mode,
                self.get_animation_style_key(),
                self.anim_frame,
                self.latest_rms,
            )
        self.root.after(33, self.animate_waveforms)

    def _tray_show(self, icon=None, item=None):
        self.root.after(0, self.show_window)

    def _tray_noop(self, icon=None, item=None):
        return None

    def _tray_quit(self, icon=None, item=None):
        self.root.after(0, self.quit_app)

    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide_to_tray(self):
        self.root.withdraw()

    def on_model_selected(self, model_name):
        self.selected_model = model_name
        self.save_settings()

    def check_idle(self):
        """Release GPU memory completely after 5 minutes of inactivity."""
        if (self.idle_unload_var.get() and self.model is not None and not self.recording
                and not self.transcribing and not self.model_loading
                and time.monotonic() - self.last_activity >= IDLE_UNLOAD_SECONDS):
            self.model = None
            self.device_used = None
            self.sleeping = True
            self.was_sleeping = False
            gc.collect()
            self.set_status("Sleeping · GPU memory released", COLOR_TEXT_MUTED)
            self.set_runtime_state("sleeping")

        # Self-heal worker threads if ever interrupted by OS power-saving
        if not hasattr(self, "poller_thread") or not self.poller_thread.is_alive():
            self.poller_thread = threading.Thread(target=self._poll_hotkeys_loop, daemon=True)
            self.poller_thread.start()
        if not hasattr(self, "listener") or not self.listener.is_alive():
            try:
                self.listener = keyboard.Listener(on_press=self.on_key_down, on_release=self.on_key_up)
                self.listener.start()
            except Exception:
                pass

        self.root.after(10_000, self.check_idle)

    def shortcut_text(self):
        return " + ".join(nice_key(key) for key in sorted(self.hotkey)) or "Set shortcut"

    @staticmethod
    def token(key):
        name = getattr(key, "name", None)
        if name:
            if name.startswith("ctrl"):
                return "ctrl"
            if name.startswith("shift"):
                return "shift"
            if name.startswith("alt"):
                return "alt"
            if name.startswith("cmd"):
                return "win"
            return f"Key.{name}"
        return str(key)

    def capture_hotkey(self):
        self.capturing = True
        self.capture_keys.clear()
        self.capture_down.clear()
        self.held_keys.clear()
        self.set_status("Press the shortcut keys together, then release · Esc cancels", COLOR_WAVE_HIGHLIGHT)
        self.shortcut_pill.configure(text="Listening…")
        self.shortcut_pill.configure(state="disabled")
        self.change_shortcut_button.configure(state="disabled")

    def on_key_down(self, key):
        token = self.token(key)
        self.held_keys.add(token)
        if self.capturing:
            if token == "Key.esc":
                self.capturing = False
                self.events.put(("hotkey_cancelled", None))
                return
            self.capture_keys.add(token)
            self.capture_down.add(token)

    def on_key_up(self, key):
        token = self.token(key)
        self.held_keys.discard(token)
        if self.capturing:
            self.capture_down.discard(token)
            if not self.capture_down and self.capture_keys:
                self.capturing = False
                selected = set(self.capture_keys)
                self.events.put(("hotkey", selected))

    def start_recording(self):
        """Trigger voice capture immediately. Runs seamlessly from background poller."""
        if self.recording:
            return
        self.recording = True
        self.last_activity = time.monotonic()
        is_waking = (self.model is None or self.sleeping or self.model_loading)
        self.was_sleeping = is_waking
        self.sleeping = False
        self.audio_parts = []
        self.events.put(("recording", is_waking))

        if self.model is None:
            self.request_model_load()
        try:
            self.stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", callback=self.audio_callback)
            self.stream.start()
        except Exception as exc:
            self.recording = False
            self.events.put(("status", f"Microphone unavailable: {exc}", "#ED8B83"))

    def stop_recording(self):
        """Stop voice capture, assemble audio and trigger GPU transcription."""
        if not self.recording:
            return
        self.recording = False
        self.last_activity = time.monotonic()
        try:
            if hasattr(self, "stream"):
                self.stream.stop()
                self.stream.close()
        except Exception:
            pass

        audio = np.concatenate(self.audio_parts).reshape(-1) if self.audio_parts else np.array([], dtype=np.float32)
        was_waking = getattr(self, "was_sleeping", False) or (self.model is None) or self.model_loading

        if len(audio) < SAMPLE_RATE // 4:
            if was_waking:
                # User pressed shortcut while sleeping/unloaded to wake up and load the model
                self.events.put(("status", "Waking GPU model…", COLOR_WAVE_ACCENT))
                self.events.put(("overlay_status", ("Waking GPU model…", "waking")))
                self.events.put(("state", "loading"))
                self.request_model_load()
            else:
                self.events.put(("status", "That was too short — hold and speak", "#F59E0B"))
                self.events.put(("hide_overlay", None))
        else:
            if self.model is None or self.transcribing or self.model_loading:
                self.pending_audios.append(audio)
                if self.model is None or self.model_loading:
                    self.events.put(("status", "Waking GPU · will transcribe immediately on ready…", COLOR_WAVE_ACCENT))
                    self.events.put(("overlay_status", ("Waking GPU — transcribing on ready…", "transcribing")))
                    self.events.put(("state", "transcribing"))
                    self.request_model_load()
                else:
                    self.events.put(("status", "Recording queued; finishing current phrase…", COLOR_WAVE_ACCENT))
                    self.events.put(("overlay_status", ("Transcribing…", "transcribing")))
                    self.events.put(("state", "transcribing"))
            else:
                self.events.put(("overlay_status", ("Transcribing…", "transcribing")))
                self.start_transcription(audio)

    def audio_callback(self, indata, frames, timing, status):
        if self.recording:
            self.audio_parts.append(indata.copy())
            block_rms = float(np.sqrt(np.mean(np.square(indata, dtype=np.float32))))
            self.latest_rms = max(min(1.0, block_rms * 18.0), self.latest_rms * 0.78)

    def request_model_load(self):
        with self.model_lock:
            if self.model_loading:
                return
            self.model_loading = True
        if not self.recording:
            self.events.put(("status", "Loading model on GPU…", "#F59E0B"))
        self.events.put(("state", "loading"))
        threading.Thread(target=self.load_model, args=(self.selected_model,), daemon=True).start()

    def load_model(self, model_name):
        try:
            if model_name not in self.model_paths:
                try:
                    self.model_paths[model_name] = download_model(model_name, local_files_only=True)
                except Exception:
                    self.model_paths[model_name] = download_model(model_name)
            local_path = self.model_paths[model_name]

            model = WhisperModel(local_path, device="cuda", compute_type="float16", local_files_only=True)

            if not self.cublas_warmed_up:
                warmup, _ = model.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32), language="en",
                                             vad_filter=False, beam_size=1)
                list(warmup)
                self.cublas_warmed_up = True

            self.model = model
            self.sleeping = False
            self.was_sleeping = False
            self.events.put(("model", (model, "cuda")))
        except Exception as gpu_error:
            self.model = None
            self.events.put(("status", f"GPU unavailable: {gpu_error}", "#ED8B83"))
        finally:
            with self.model_lock:
                self.model_loading = False

    def reload_model(self, _value=None):
        if self.transcribing:
            self.set_status("Wait for the current transcription to finish before reloading", "#F59E0B")
            return
        self.save_settings()
        self.model = None
        self.device_used = None
        self.reload_button.configure(state="disabled")
        self.request_model_load()

    def start_transcription(self, audio):
        self.transcribing = True
        self.last_activity = time.monotonic()
        self.events.put(("status", "Transcribing on NVIDIA GPU…", COLOR_WAVE_ACCENT))
        self.events.put(("overlay_status", ("Transcribing…", "transcribing")))
        self.events.put(("state", "transcribing"))
        threading.Thread(target=self.transcribe, args=(audio, self.model), daemon=True).start()

    def transcribe(self, audio, model):
        try:
            segments, info = model.transcribe(audio, language=None, task="transcribe", multilingual=True,
                                              vad_filter=True, beam_size=5)
            result = "".join(segment.text for segment in segments).strip()
            detected = {"en": "English", "hi": "Hindi"}.get(info.language, info.language.upper())
            self.events.put(("result", (result, detected, "cuda")))
        except Exception as exc:
            self.model = None
            self.device_used = None
            self.sleeping = True
            self.events.put(("status", f"GPU transcription error: {exc}", "#ED8B83"))
        finally:
            self.transcribing = False

    def paste_text(self, text):
        """Simulate Ctrl+V directly via Windows keybd_event to paste into the active app."""
        if not self.paste_var.get() or not text:
            return
        try:
            import pyperclip
            pyperclip.copy(text)
            time.sleep(0.08)
            user32 = ctypes.windll.user32
            # Explicitly release any physical modifier keys before sending Ctrl+V
            user32.keybd_event(0x11, 0, 0x0002, 0)  # VK_CONTROL up
            user32.keybd_event(0x10, 0, 0x0002, 0)  # VK_SHIFT up
            user32.keybd_event(0x12, 0, 0x0002, 0)  # VK_MENU (Alt) up
            time.sleep(0.02)
            self.suppress_shortcut = True
            # Send Ctrl+V
            user32.keybd_event(0x11, 0, 0, 0)       # Ctrl down
            user32.keybd_event(0x56, 0, 0, 0)       # 'V' down
            user32.keybd_event(0x56, 0, 0x0002, 0)  # 'V' up
            user32.keybd_event(0x11, 0, 0x0002, 0)  # Ctrl up
            time.sleep(0.05)
            self.suppress_shortcut = False
        except Exception as exc:
            self.suppress_shortcut = False
            self.set_status(f"Copied, but paste failed: {exc}", "#ED8B83")
        finally:
            self.suppress_shortcut = False

    def save_settings(self, _value=None):
        self.settings.update(
            hotkey=sorted(self.hotkey),
            model=self.model_var.get(),
            animation_style=self.animation_style_var.get(),
            paste=self.paste_var.get(),
            idle_unload=self.idle_unload_var.get(),
            show_overlay=self.overlay_var.get(),
        )
        try:
            CONFIG_PATH.write_text(json.dumps(self.settings, indent=2), encoding="utf-8")
        except OSError:
            pass

    def set_status(self, text, color):
        self.status_var.set(text)
        self._draw_status_icon(color)

    def process_events(self):
        while not self.events.empty():
            item = self.events.get()
            kind = item[0]
            payload = item[1] if len(item) > 1 else None
            extra = item[2:] if len(item) > 2 else ()

            if kind == "status":
                color = extra[0] if extra else COLOR_TEXT_MUTED
                self.set_status(payload, color)
                if "GPU unavailable" in payload or "GPU transcription error" in payload:
                    self.model_loading = False
                    if self.model is None:
                        self.sleeping = True
                    self.set_runtime_state("error")
                    self.reload_button.configure(state="normal")
                    self._hide_overlay()
                elif "too short" in payload.lower():
                    if self.model is not None:
                        self.set_runtime_state("ready")
                    self._hide_overlay()
                elif "microphone unavailable" in payload.lower():
                    if self.model is not None:
                        self.set_runtime_state("ready")
                    else:
                        self.set_runtime_state("error")
                    self._hide_overlay()
            elif kind == "state":
                self.set_runtime_state(payload)
            elif kind == "recording":
                is_waking = payload
                if is_waking:
                    self.set_status("Listening… (Waking GPU in background)", COLOR_WAVE_HIGHLIGHT)
                    self._show_overlay("Waking GPU — listening…", mode="waking")
                else:
                    self.set_status("Listening… release to finish", COLOR_WAVE_HIGHLIGHT)
                    self._show_overlay("Listening — release to finish", mode="listening")
                self.set_runtime_state("listening")
            elif kind == "model":
                self.model, self.device_used = payload
                self.model_loading = False
                self.sleeping = False
                self.was_sleeping = False
                self.last_activity = time.monotonic()
                if self.recording:
                    self.set_runtime_state("listening")
                    self.set_status("Listening… release to finish", COLOR_WAVE_HIGHLIGHT)
                elif self.pending_audios:
                    self.set_runtime_state("transcribing")
                    self.start_transcription(self.pending_audios.pop(0))
                else:
                    self.set_runtime_state("ready")
                    self.set_status("Ready · NVIDIA GPU active", "#10B981")
                    if hasattr(self, "floating_overlay") and self.floating_overlay.is_visible():
                        self._show_overlay("Ready · GPU active", mode="done")
                        self.root.after(1400, self._hide_overlay)
                self.reload_button.configure(state="normal")
            elif kind == "overlay_status":
                text, mode = payload
                self._show_overlay(text, mode=mode)
            elif kind == "hide_overlay":
                self._hide_overlay()
            elif kind == "hotkey":
                self.hotkey = payload
                self.shortcut_pill.configure(text=self.shortcut_text())
                self.shortcut_pill.configure(state="normal")
                self.change_shortcut_button.configure(state="normal")
                self.save_settings()
                self.set_status("Shortcut updated", "#10B981")
            elif kind == "hotkey_cancelled":
                self.shortcut_pill.configure(text=self.shortcut_text())
                self.shortcut_pill.configure(state="normal")
                self.change_shortcut_button.configure(state="normal")
                self.set_status("Shortcut change cancelled", COLOR_TEXT_MUTED)
            elif kind == "result":
                result, detected, device = payload
                self.last_activity = time.monotonic()
                if result:
                    self.transcript.configure(state="normal")
                    self.transcript.delete("1.0", "end")
                    self.transcript.insert("1.0", result)
                    self.transcript.configure(state="disabled")
                    if self.recording:
                        self.set_status("Listening… release to finish", COLOR_WAVE_HIGHLIGHT)
                        self.set_runtime_state("listening")
                    else:
                        dev_name = device.upper() if device else "GPU"
                        self.set_status(f"{detected} detected · {dev_name} · Ready", "#10B981")
                        self.set_runtime_state("ready")
                        self._show_overlay("Done", mode="done")
                        self.root.after(650, self._hide_overlay)
                    if self.paste_var.get():
                        self.root.after(120, self.paste_text, result)
                else:
                    if not self.recording:
                        self.set_status("No speech detected — try again", "#F59E0B")
                        self.set_runtime_state("ready")
                        self._hide_overlay()
                if self.pending_audios and self.model is not None and not self.transcribing:
                    self.start_transcription(self.pending_audios.pop(0))

        self.root.after(25, self.process_events)

    def quit_app(self):
        self._stop_requested = True
        self.save_settings()
        self.listener.stop()
        self._hide_overlay()
        if hasattr(self, "floating_overlay"):
            self.floating_overlay.destroy()
        if hasattr(self, "tray"):
            self.tray.stop()
        self.root.destroy()


if __name__ == "__main__":
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    root = ctk.CTk()
    RorishperFlow(root)
    root.mainloop()
