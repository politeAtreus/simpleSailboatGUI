"""
Sailboat Ground Station Monitor
================================
A CustomTkinter (Windows) GUI for monitoring the serial output of the
STM32 joystick controller used in the autonomous boat project.

It parses TWO kinds of lines that the controller prints over the COM port
(this is the ST-Link / debug UART you are watching in PuTTY):

1) Controller status line (printed continuously):

       sail=-19  rudder=0  | dropped=0  overruns=0

   - sail     : left-joystick sail command     (-45 .. +45)
   - rudder   : right-joystick rudder command   (-45 .. +45)
   - dropped  : dropped XBee packet counter
   - overruns : UART / packet overrun counter

2) Radio telemetry echoed back from the boat PCB (only when in range):

       XBee RX: {"tb":0,"tlat":0,...,"wa":0"sa":347,}

   NOTE: in the screenshot this payload is *almost* JSON but is slightly
   malformed -- there is a missing comma between "wa":0 and "sa":347 and a
   trailing comma before the closing brace. So we deliberately do NOT use
   json.loads(); we use a tolerant "key":value regex that survives the
   missing/extra commas and any field ordering.

Requirements:
    pip install customtkinter pyserial

Run:
    python sailboat_monitor.py
"""

import csv
import math
import os
import queue
import re
import threading
import time
import tkinter as tk
from collections import deque
from datetime import datetime

import customtkinter as ctk
import serial
import serial.tools.list_ports

# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

# sail=-19  rudder=0  | dropped=0  overruns=0
SAIL_RUDDER_RE = re.compile(
    r"sail\s*=\s*(-?\d+)\s+rudder\s*=\s*(-?\d+)\s*\|\s*"
    r"dropped\s*=\s*(\d+)\s+overruns\s*=\s*(\d+)"
)

# Tolerant "key":value matcher.  Handles ints, floats and negatives.
# It ignores commas entirely, so the malformed JSON in the screenshot
# (missing comma / trailing comma) parses cleanly.
KV_RE = re.compile(r'"([A-Za-z_]\w*)"\s*:\s*(-?\d+(?:\.\d+)?)')


def parse_sail_rudder(line: str):
    """Return dict for a controller status line, or None."""
    m = SAIL_RUDDER_RE.search(line)
    if not m:
        return None
    return {
        "sail": int(m.group(1)),
        "rudder": int(m.group(2)),
        "dropped": int(m.group(3)),
        "overruns": int(m.group(4)),
    }


def parse_xbee(line: str):
    """Return dict of telemetry fields for an 'XBee RX:' line, or None."""
    if "XBee RX" not in line:
        return None
    payload = line.split("XBee RX:", 1)[1]
    pairs = KV_RE.findall(payload)
    if not pairs:
        return None
    out = {}
    for key, raw in pairs:
        out[key] = float(raw) if "." in raw else int(raw)
    return out


# Telemetry field metadata.  The human-readable labels are my best guess
# at your protocol (t* = target/setpoint, c* = current boat state); they
# are easy to rename here without touching anything else.
TELEMETRY_FIELDS = [
    # key,    label,                 group
    ("tb",   "Target Bearing",       "Target / Setpoints"),
    ("tlat", "Target Latitude",      "Target / Setpoints"),
    ("tlon", "Target Longitude",     "Target / Setpoints"),
    ("tsa",  "Target Sail Angle",    "Target / Setpoints"),
    ("tfa",  "Target Flap Angle",    "Target / Setpoints"),
    ("tra",  "Target Rudder Angle",  "Target / Setpoints"),
    ("clat", "Current Latitude",     "Boat State"),
    ("clon", "Current Longitude",    "Boat State"),
    ("cb",   "Current Bearing",      "Boat State"),
    ("wa",   "Wind Angle",           "Boat State"),
    ("sa",   "Sail Angle",           "Boat State"),
]

DEGREE_FIELDS = {"tb", "tsa", "tfa", "tra", "cb", "wa", "sa"}
LATLON_FIELDS = {"tlat", "tlon", "clat", "clon"}


def fmt_value(key: str, v) -> str:
    if key in LATLON_FIELDS:
        return f"{float(v):.6f}"
    if isinstance(v, float):
        s = f"{v:.2f}"
    else:
        s = str(v)
    if key in DEGREE_FIELDS:
        s += "\u00b0"
    return s


def is_stlink(port) -> bool:
    """True if a pyserial ListPortInfo looks like an ST-Link Virtual COM Port.

    Matches on the description text (covers the various wordings Windows uses:
    'STMicroelectronics STLink Virtual COM Port', 'ST-Link', etc.) and, as a
    fallback, on the STMicroelectronics USB vendor ID 0x0483.
    """
    desc = (port.description or "").lower()
    if "stlink" in desc or "st-link" in desc or "st link" in desc:
        return True
    if getattr(port, "vid", None) == 0x0483:
        return True
    return False


# --------------------------------------------------------------------------- #
# Centered bar widget (for sail / rudder, which swing -45 .. +45 about zero)
# --------------------------------------------------------------------------- #

def _resolve_color(color):
    """Pick the light or dark variant from a CTk colour.

    CustomTkinter colours are often a [light, dark] pair; raw tkinter canvases
    can't use that, so resolve to a single value for the current mode.
    """
    if isinstance(color, (list, tuple)):
        return color[0] if ctk.get_appearance_mode() == "Light" else color[1]
    return color


class CenteredBar(tk.Canvas):
    """A horizontal gauge that fills left or right from a centre line.

    Because this is a raw tkinter Canvas (not a CTk widget) it does NOT follow
    set_appearance_mode automatically, so it recomputes its colours from the
    current mode on every draw and exposes refresh_theme() for the toggle.
    """

    def __init__(self, master, width=360, height=42, vmin=-45, vmax=45,
                 fill="#3b8ed0", **kwargs):
        super().__init__(master, width=width, height=height,
                         highlightthickness=0, **kwargs)
        self.master_frame = master
        self.w, self.h = width, height
        self.vmin, self.vmax = vmin, vmax
        self.fill_color = fill
        self.value = 0
        self.bind("<Configure>", lambda e: self._draw())
        self.refresh_theme()

    def set_value(self, v):
        self.value = max(self.vmin, min(self.vmax, v))
        self._draw()

    def refresh_theme(self):
        """Match the canvas background to the parent card and redraw."""
        self.configure(bg=self._bg_color())
        self._draw()

    def _bg_color(self):
        try:
            c = _resolve_color(self.master_frame.cget("fg_color"))
        except Exception:
            c = None
        if not c or c == "transparent":
            return "#2b2b2b" if ctk.get_appearance_mode() == "Dark" else "#dbdbdb"
        return c

    def _palette(self):
        if ctk.get_appearance_mode() == "Light":
            return {"track": "#c9c9cf", "tick": "#9a9aa5",
                    "center": "#55555f", "knob": "#ffffff"}
        return {"track": "#2a2a36", "tick": "#4a4a5a",
                "center": "#8a8aa0", "knob": "#ffffff"}

    def _draw(self):
        self.delete("all")
        pal = self._palette()
        w = self.winfo_width() or self.w
        h = self.winfo_height() or self.h
        pad = 12
        track_h = 14
        cx = w / 2
        cy = h / 2
        left, right = pad, w - pad
        top, bot = cy - track_h / 2, cy + track_h / 2

        # background track
        self.create_rectangle(left, top, right, bot, fill=pal["track"],
                              outline="")
        # ticks at min / centre / max
        for frac in (0.0, 0.5, 1.0):
            x = left + frac * (right - left)
            self.create_line(x, top - 5, x, bot + 5, fill=pal["tick"], width=1)
        # centre line emphasised
        self.create_line(cx, top - 7, cx, bot + 7, fill=pal["center"], width=2)

        # fill from centre toward the value
        if self.value >= 0:
            x = cx + (self.value / self.vmax) * (right - cx)
            self.create_rectangle(cx, top, x, bot, fill=self.fill_color,
                                  outline="")
        else:
            x = cx - (self.value / self.vmin) * (cx - left)
            self.create_rectangle(x, top, cx, bot, fill=self.fill_color,
                                  outline="")
        # knob
        r = 7
        self.create_oval(x - r, cy - r, x + r, cy + r,
                         fill=self._palette()["knob"], outline=self.fill_color,
                         width=2)


# --------------------------------------------------------------------------- #
# Boat view widget (top-down schematic: sail pivots at centre, rudder at stern)
# --------------------------------------------------------------------------- #

class BoatView(tk.Canvas):
    """Top-down boat schematic. Bow points up.

    The sail rotates about the mast at the centre of the hull and the rudder
    rotates about its pivot at the stern, both driven by the live joystick
    angles (-45 .. +45 deg). Positive angle swings to starboard (right).
    """

    SAIL_COLOR = "#3b8ed0"     # matches the sail gauge / value
    RUDDER_COLOR = "#e8a33d"   # matches the rudder gauge / value

    def __init__(self, master, width=300, height=240, **kwargs):
        super().__init__(master, width=width, height=height,
                         highlightthickness=0, **kwargs)
        self.master_frame = master
        self.w, self.h = width, height
        self.sail_angle = 0.0
        self.rudder_angle = 0.0
        self.bind("<Configure>", lambda e: self._draw())
        self.refresh_theme()

    def set_angles(self, sail, rudder):
        self.sail_angle = float(sail)
        self.rudder_angle = float(rudder)
        self._draw()

    def refresh_theme(self):
        self.configure(bg=self._bg_color())
        self._draw()

    def _bg_color(self):
        try:
            c = _resolve_color(self.master_frame.cget("fg_color"))
        except Exception:
            c = None
        if not c or c == "transparent":
            return "#2b2b2b" if ctk.get_appearance_mode() == "Dark" else "#dbdbdb"
        return c

    def _palette(self):
        if ctk.get_appearance_mode() == "Light":
            return {"hull": "#c2c6cf", "outline": "#5a5a66", "pivot": "#3a3a44",
                    "label": "#55555f"}
        return {"hull": "#3a3a48", "outline": "#9a9aac", "pivot": "#dcdce6",
                "label": "#9a9aa5"}

    @staticmethod
    def _rotate(px, py, cx, cy, deg):
        """Rotate (px,py) about (cx,cy). 0 deg = straight aft (downward),
        positive = toward starboard (screen right) to match the gauges, which
        fill right for positive values. The angle is negated because the canvas
        y-axis points down."""
        r = math.radians(-deg)
        dx, dy = px - cx, py - cy
        rx = dx * math.cos(r) - dy * math.sin(r)
        ry = dx * math.sin(r) + dy * math.cos(r)
        return cx + rx, cy + ry

    def _draw(self):
        self.delete("all")
        pal = self._palette()
        w = self.winfo_width() or self.w
        h = self.winfo_height() or self.h
        cx = w / 2
        cy = h * 0.45          # shift up so the rudder has room below
        L = h * 0.30           # hull half-length
        W = min(w * 0.16, L * 0.55)  # hull half-width

        # ---- hull (smoothed polygon, bow at top) ----
        hull = [
            cx, cy - L,                        # bow tip
            cx + W * 0.85, cy - L * 0.40,
            cx + W, cy + L * 0.15,
            cx + W * 0.70, cy + L * 0.80,
            cx + W * 0.45, cy + L,             # starboard stern
            cx - W * 0.45, cy + L,             # port stern
            cx - W * 0.70, cy + L * 0.80,
            cx - W, cy + L * 0.15,
            cx - W * 0.85, cy - L * 0.40,
        ]
        self.create_polygon(hull, fill=pal["hull"], outline=pal["outline"],
                            width=2, smooth=True)

        # ---- mast + sail (pivots at hull centre) ----
        # 0 deg = bow (boom points forward, straight up). Increasing angle
        # rotates clockwise (1,2,3...). The boom base point below is aft of the
        # mast: '180 - angle' puts 0 at the bow and makes the sweep clockwise.
        # Shared by both boat tiles.
        mast_x, mast_y = cx, cy
        boom_len = L * 0.85
        tip = self._rotate(mast_x, mast_y + boom_len, mast_x, mast_y,
                           180.0 - self.sail_angle)
        self.create_line(mast_x, mast_y, tip[0], tip[1],
                        fill=self.SAIL_COLOR, width=7, capstyle="round")
        self.create_oval(mast_x - 5, mast_y - 5, mast_x + 5, mast_y + 5,
                        fill=pal["pivot"], outline="")

        # ---- rudder (pivots at the stern) ----
        rud_x, rud_y = cx, cy + L
        rud_len = L * 0.45
        rtip = self._rotate(rud_x, rud_y + rud_len, rud_x, rud_y,
                           self.rudder_angle)
        self.create_line(rud_x, rud_y, rtip[0], rtip[1],
                        fill=self.RUDDER_COLOR, width=6, capstyle="round")
        self.create_oval(rud_x - 4, rud_y - 4, rud_x + 4, rud_y + 4,
                        fill=pal["pivot"], outline="")

        # ---- small captions ----
        self.create_text(cx, cy - L - 10, text="BOW", fill=pal["label"],
                        font=("TkDefaultFont", 8))
        self.create_text(cx + W + 14, mast_y, text=f"Sail {self.sail_angle:.0f}\u00b0",
                        fill=self.SAIL_COLOR, font=("TkDefaultFont", 9, "bold"),
                        anchor="w")
        self.create_text(cx + 12, rud_y + rud_len * 0.6,
                        text=f"Rudder {self.rudder_angle:.0f}\u00b0",
                        fill=self.RUDDER_COLOR,
                        font=("TkDefaultFont", 9, "bold"), anchor="w")


# --------------------------------------------------------------------------- #
# Pseudo-3D boat view (orthographic projection on a plain tkinter Canvas)
# --------------------------------------------------------------------------- #

def _rot_z(p, deg):
    """Yaw a 3D point about the vertical (z) axis through the origin."""
    a = math.radians(deg)
    x, y, z = p
    return (x * math.cos(a) - y * math.sin(a),
            x * math.sin(a) + y * math.cos(a), z)


def _rot_z_about(p, deg, ox, oy):
    """Yaw a 3D point about a vertical axis through (ox, oy)."""
    a = math.radians(deg)
    x, y, z = p
    dx, dy = x - ox, y - oy
    return (ox + dx * math.cos(a) - dy * math.sin(a),
            oy + dx * math.sin(a) + dy * math.cos(a), z)


class Boat3DView(tk.Canvas):
    """Lightweight 3/4-view of the boat: hull, keel, mast/sail and rudder.

    No 3D engine -- points are rotated for the boat's heading and the camera,
    then orthographically projected and drawn back-to-front (painter's order).
    Driven by heading (cb), sail angle (sa) and rudder angle (tra).
    """

    SAIL_COLOR = "#3b8ed0"
    RUDDER_COLOR = "#e8a33d"
    AZ = 35.0   # camera azimuth (deg)
    EL = 26.0   # camera elevation (deg)

    def __init__(self, master, width=460, height=460, **kwargs):
        super().__init__(master, width=width, height=height,
                         highlightthickness=0, **kwargs)
        self.master_frame = master
        self.w, self.h = width, height
        self.heading = 0.0
        self.sail_angle = 0.0
        self.rudder_angle = 0.0
        self.bind("<Configure>", lambda e: self._draw())
        self.refresh_theme()

    def set_state(self, heading, sail, rudder):
        self.heading = float(heading)
        self.sail_angle = float(sail)
        self.rudder_angle = float(rudder)
        self._draw()

    def refresh_theme(self):
        self.configure(bg=self._bg_color())
        self._draw()

    def _bg_color(self):
        try:
            c = _resolve_color(self.master_frame.cget("fg_color"))
        except Exception:
            c = None
        if not c or c == "transparent":
            return "#2b2b2b" if ctk.get_appearance_mode() == "Dark" else "#dbdbdb"
        return c

    def _palette(self):
        if ctk.get_appearance_mode() == "Light":
            return {"water": "#b9c4cf", "deck": "#c8ccd4", "hull": "#aeb3bd",
                    "hull_dark": "#9398a3", "keel": "#6f7480", "mast": "#5a5a66",
                    "label": "#55555f", "outline": "#5a5a66"}
        return {"water": "#2f3a44", "deck": "#54596a", "hull": "#454a5b",
                "hull_dark": "#363a48", "keel": "#2b2e3a", "mast": "#c8c8d4",
                "label": "#9a9aa5", "outline": "#1c1c24"}

    def _project(self, p, scale, cx, cy):
        x, y, z = p
        az = math.radians(self.AZ)
        el = math.radians(self.EL)
        x1 = x * math.cos(az) - y * math.sin(az)
        y1 = x * math.sin(az) + y * math.cos(az)
        up = z * math.cos(el) - y1 * math.sin(el)        # screen vertical (+up)
        depth = z * math.sin(el) + y1 * math.cos(el)     # into-screen distance
        return (cx + scale * x1, cy - scale * up, depth)

    def _draw(self):
        self.delete("all")
        pal = self._palette()
        w = self.winfo_width() or self.w
        h = self.winfo_height() or self.h
        cx, cy = w / 2.0, h * 0.60
        scale = min(w, h) * 0.135   # smaller so the tall wing sail fits

        def P(p):
            return self._project(p, scale, cx, cy)

        # ---- water grid (world plane z=0) ----
        g = 3.0
        step = 0.6
        n = int(g / step)
        for i in range(-n, n + 1):
            a = P((i * step, -g, 0));  b = P((i * step, g, 0))
            self.create_line(a[0], a[1], b[0], b[1], fill=pal["water"], width=1)
            a = P((-g, i * step, 0));  b = P((g, i * step, 0))
            self.create_line(a[0], a[1], b[0], b[1], fill=pal["water"], width=1)

        # ---- build boat geometry in boat frame (y=fwd/bow, x=stbd, z=up) ----
        # Long, slender monohull (see reference photos).
        deck_z, bot_z = 0.16, -0.22
        top = [(0, 1.85, deck_z), (0.20, 1.05, deck_z), (0.30, 0.0, deck_z),
               (0.26, -1.05, deck_z), (0.15, -1.6, deck_z),
               (-0.15, -1.6, deck_z), (-0.26, -1.05, deck_z),
               (-0.30, 0.0, deck_z), (-0.20, 1.05, deck_z)]
        bot = [(0, 1.7, bot_z), (0.10, 1.05, bot_z), (0.15, 0.0, bot_z),
               (0.13, -1.05, bot_z), (0.07, -1.5, bot_z),
               (-0.07, -1.5, bot_z), (-0.13, -1.05, bot_z),
               (-0.15, 0.0, bot_z), (-0.10, 1.05, bot_z)]

        faces = []  # (list_of_3d_pts, fill, outline)
        # hull side panels
        m = len(top)
        for i in range(m):
            j = (i + 1) % m
            faces.append(([top[i], top[j], bot[j], bot[i]],
                          pal["hull"], pal["outline"]))
        # deck (top) and hull bottom
        faces.append((list(top), pal["deck"], pal["outline"]))
        faces.append((list(bot), pal["hull_dark"], pal["outline"]))

        # deep fin keel (vertical, x=0 plane, roughly amidships, hangs well down)
        keel_y, keel_depth = 0.05, -1.85
        keel = [(0, keel_y + 0.32, bot_z), (0, keel_y - 0.34, bot_z),
                (0, keel_y - 0.10, keel_depth), (0, keel_y + 0.10, keel_depth)]
        faces.append((keel, pal["keel"], pal["keel"]))

        # rudder (shorter fin at the stern; yaws with the rudder angle)
        rpx, rpy = 0.0, -1.50
        rud = [(0, -1.44, -0.16), (0, -1.60, -0.16),
               (0, -1.54, -0.92), (0, -1.38, -0.92)]
        rud = [_rot_z_about(p, self.rudder_angle, rpx, rpy) for p in rud]
        faces.append((rud, self.RUDDER_COLOR, self.RUDDER_COLOR))

        # wind-sensor pole near the bow (short black pole + sensor head)
        wp_x, wp_y = 0.0, 0.95
        sens_base = (wp_x, wp_y, deck_z)
        sens_top = (wp_x, wp_y, deck_z + 0.55)

        # rigid WING SAIL: a tall rectangular panel that pivots about the mast.
        # 0 deg = bow, clockwise positive; the chord swings with the sail angle.
        mast_y = 0.0
        chord = 0.62
        wing_top = 2.7
        th = math.radians(self.sail_angle)
        dx, dy = math.sin(th), math.cos(th)         # chord (trailing) direction
        le_b = (0, mast_y, deck_z)                  # leading edge, bottom
        le_t = (0, mast_y, wing_top)                # leading edge, top
        te_t = (chord * dx, mast_y + chord * dy, wing_top)
        te_b = (chord * dx, mast_y + chord * dy, deck_z)
        faces.append(([le_b, le_t, te_t, te_b], self.SAIL_COLOR, "#1c2e3c"))

        # ---- apply heading yaw, project, sort back-to-front, draw ----
        def H(p):
            return _rot_z(p, -self.heading)

        drawn = []
        for pts3d, fill, outline in faces:
            proj = [P(H(p)) for p in pts3d]
            depth = sum(q[2] for q in proj) / len(proj)
            flat = []
            for q in proj:
                flat.extend((q[0], q[1]))
            drawn.append((depth, flat, fill, outline))
        # draw farthest first (largest depth)
        drawn.sort(key=lambda t: t[0], reverse=True)
        for _depth, flat, fill, outline in drawn:
            self.create_polygon(flat, fill=fill, outline=outline, width=1)

        # mast (wing leading edge) on top, plus the bow wind-sensor pole
        a = P(H(le_b)); b = P(H(le_t))
        self.create_line(a[0], a[1], b[0], b[1], fill=pal["mast"], width=3)
        a = P(H(sens_base)); b = P(H(sens_top))
        self.create_line(a[0], a[1], b[0], b[1], fill=pal["mast"], width=2)
        self.create_oval(b[0] - 4, b[1] - 4, b[0] + 4, b[1] + 4,
                        fill=pal["deck"], outline=pal["mast"])

        # ---- captions ----
        self.create_text(12, 14, anchor="nw",
                        text=f"Heading {self.heading:.0f}\u00b0",
                        fill=pal["label"], font=("TkDefaultFont", 10, "bold"))
        self.create_text(12, 32, anchor="nw",
                        text=f"Sail {self.sail_angle:.0f}\u00b0",
                        fill=self.SAIL_COLOR, font=("TkDefaultFont", 10, "bold"))
        self.create_text(12, 50, anchor="nw",
                        text=f"Rudder {self.rudder_angle:.0f}\u00b0",
                        fill=self.RUDDER_COLOR,
                        font=("TkDefaultFont", 10, "bold"))


# --------------------------------------------------------------------------- #
# Wind rose + rolling trend plot (plain tkinter canvases)
# --------------------------------------------------------------------------- #

def _canvas_bg(master_frame):
    """Resolve a canvas background that matches its parent CTk frame."""
    try:
        c = _resolve_color(master_frame.cget("fg_color"))
    except Exception:
        c = None
    if not c or c == "transparent":
        return "#2b2b2b" if ctk.get_appearance_mode() == "Dark" else "#dbdbdb"
    return c


class WindRose(tk.Canvas):
    """Compass-style dial for the wind angle (wa), bow at the top.

    wa is treated as degrees clockwise from the bow (0 = wind from dead ahead).
    """

    COLOR = "#5fd0a0"

    def __init__(self, master, size=150, **kwargs):
        super().__init__(master, width=size, height=size,
                         highlightthickness=0, **kwargs)
        self.master_frame = master
        self.size = size
        self.wind = 0.0
        self.bind("<Configure>", lambda e: self._draw())
        self.refresh_theme()

    def set_wind(self, wa):
        self.wind = float(wa) % 360.0
        self._draw()

    def refresh_theme(self):
        self.configure(bg=_canvas_bg(self.master_frame))
        self._draw()

    def _palette(self):
        if ctk.get_appearance_mode() == "Light":
            return {"ring": "#9a9aa5", "tick": "#7a7a85", "label": "#55555f",
                    "boat": "#5a5a66"}
        return {"ring": "#54596a", "tick": "#6c6f80", "label": "#9a9aa5",
                "boat": "#c8c8d4"}

    def _draw(self):
        self.delete("all")
        pal = self._palette()
        w = self.winfo_width() or self.size
        h = self.winfo_height() or self.size
        cx, cy = w / 2.0, h / 2.0 + 4
        r = min(w, h) / 2.0 - 16

        self.create_oval(cx - r, cy - r, cx + r, cy + r,
                        outline=pal["ring"], width=2)
        # cardinal ticks
        for ang in (0, 90, 180, 270):
            a = math.radians(ang)
            x1 = cx + (r - 6) * math.sin(a); y1 = cy - (r - 6) * math.cos(a)
            x2 = cx + r * math.sin(a);       y2 = cy - r * math.cos(a)
            self.create_line(x1, y1, x2, y2, fill=pal["tick"], width=1)
        self.create_text(cx, cy - r - 7, text="BOW", fill=pal["label"],
                        font=("TkDefaultFont", 8))

        # boat indicator at centre (small triangle pointing to the bow / up)
        self.create_polygon(cx, cy - 9, cx - 5, cy + 7, cx + 5, cy + 7,
                           fill=pal["boat"], outline="")

        # wind arrow: wind comes FROM bearing `wind` (clockwise from bow)
        a = math.radians(self.wind)
        fx = cx + r * math.sin(a); fy = cy - r * math.cos(a)
        self.create_line(fx, fy, cx, cy, fill=self.COLOR, width=3,
                        arrow="last", arrowshape=(10, 12, 5))
        self.create_text(cx, cy + r + 8, text=f"Wind {self.wind:.0f}\u00b0",
                        fill=self.COLOR, font=("TkDefaultFont", 10, "bold"))


class TrendPlot(tk.Canvas):
    """Rolling 0-360 deg time-series of wind angle, heading and sail angle."""

    CHANNELS = [("wa", "Wind", "#5fd0a0"),
                ("cb", "Heading", "#c08ae0"),
                ("sa", "Sail", "#3b8ed0")]
    MAXLEN = 150  # ~2.5 min at 1 Hz telemetry

    def __init__(self, master, width=320, height=150, **kwargs):
        super().__init__(master, width=width, height=height,
                         highlightthickness=0, **kwargs)
        self.master_frame = master
        self.w, self.h = width, height
        self.data = {k: deque(maxlen=self.MAXLEN) for k, _, _ in self.CHANNELS}
        self.bind("<Configure>", lambda e: self._draw())
        self.refresh_theme()

    def add_sample(self, wa, cb, sa):
        self.data["wa"].append(float(wa) % 360.0)
        self.data["cb"].append(float(cb) % 360.0)
        self.data["sa"].append(float(sa) % 360.0)
        self._draw()

    def refresh_theme(self):
        self.configure(bg=_canvas_bg(self.master_frame))
        self._draw()

    def _palette(self):
        if ctk.get_appearance_mode() == "Light":
            return {"axis": "#9a9aa5", "grid": "#cfcfd6", "label": "#55555f"}
        return {"axis": "#54596a", "grid": "#333642", "label": "#9a9aa5"}

    def _draw(self):
        self.delete("all")
        pal = self._palette()
        w = self.winfo_width() or self.w
        h = self.winfo_height() or self.h
        left, right, top, bot = 30, w - 8, 8, h - 6

        for val in (0, 90, 180, 270, 360):
            y = bot - (val / 360.0) * (bot - top)
            self.create_line(left, y, right, y, fill=pal["grid"], width=1)
            self.create_text(left - 4, y, text=str(val), anchor="e",
                            fill=pal["label"], font=("TkDefaultFont", 7))
        self.create_line(left, top, left, bot, fill=pal["axis"], width=1)
        self.create_line(left, bot, right, bot, fill=pal["axis"], width=1)

        n = len(self.data["wa"])
        span = max(n - 1, 1)
        legx = left + 6
        for key, label, color in self.CHANNELS:
            vals = list(self.data[key])
            if len(vals) >= 2:
                pts = []
                for i, v in enumerate(vals):
                    x = left + (i / span) * (right - left)
                    y = bot - (v / 360.0) * (bot - top)
                    pts.extend((x, y))
                self.create_line(*pts, fill=color, width=2)
            cur = f"{vals[-1]:.0f}\u00b0" if vals else "--"
            self.create_text(legx, top + 6, text=f"{label} {cur}", anchor="w",
                            fill=color, font=("TkDefaultFont", 8, "bold"))
            legx += 82


# --------------------------------------------------------------------------- #
# Serial reader thread
# --------------------------------------------------------------------------- #

class SerialReader(threading.Thread):
    """Reads complete lines off the serial port and pushes them to a queue.

    Runs in a background thread so the GUI never blocks.  All widget updates
    happen on the main thread via App.poll_queue(), because tkinter is not
    thread-safe.
    """

    def __init__(self, ser, out_queue, stop_event):
        super().__init__(daemon=True)
        self.ser = ser
        self.out_queue = out_queue
        self.stop_event = stop_event

    def run(self):
        while not self.stop_event.is_set():
            try:
                raw = self.ser.readline()  # blocks up to the port timeout
            except (serial.SerialException, OSError) as e:
                self.out_queue.put(("error", f"Serial error: {e}"))
                return
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace").strip("\r\n")
            if line:
                self.out_queue.put(("line", line))


# --------------------------------------------------------------------------- #
# Main application
# --------------------------------------------------------------------------- #

BAUD_RATES = ["9600", "19200", "38400", "57600", "115200", "230400", "460800"]
MAX_LOG_LINES = 2000
PORT_POLL_MS = 1500  # how often to scan for COM-port hot-plug / unplug

# The sail is a continuous-rotation drive, not a positional servo: a negative
# command spins it anti-clockwise (viewed from above), a positive command spins
# it clockwise, both at a constant slew rate. The COMMANDED boat integrates this
# over real time instead of treating the stick value as an angle.
SAIL_ROTATION_RATE_DPS = 72.0  # degrees per second
SAIL_CMD_DEADBAND = 2.0        # |command| below this is treated as "stop"

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")


class App(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Sailboat Ground Station Monitor")
        self.geometry("1120x760")
        self.minsize(960, 640)

        # serial state
        self.ser = None
        self.reader = None
        self.stop_event = None
        self.queue = queue.Queue()
        self.port_map = {}  # display string -> device name
        self.telemetry_labels = {}  # key -> value label widget
        self.last_rx_time = None
        self.known_devices = None    # set of COM device names; None until 1st scan
        self.auto_target = None      # ST-Link device we want to auto-connect to
        self._auto_fail_logged = set()  # devices whose open failure we've logged
        # Commanded-sail animation state (continuous-rotation model)
        self.cmd_sail_input = 0.0    # latest sail stick command (-45..+45)
        self.cmd_sail_angle = 0.0    # integrated displayed sail angle
        self.cmd_rudder = 0.0        # latest rudder command (positional)
        self._last_sail_tick = None  # time.monotonic() of last animation frame
        # 3D + GPS map window (created on demand)
        self.win3d = None
        self.view3d = None
        self.mapview = None
        self.boat_marker = None
        self.wp_marker = None
        self.gps_track = []          # [(lat, lon, datetime)] recorded always
        # CSV recording
        self.csv_file = None
        self.csv_writer = None
        self.csv_rows = 0
        self.csv_path = None

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=2)   # panels expand
        self.grid_rowconfigure(2, weight=1, minsize=155)  # log always 5-6 lines

        self._build_connection_bar()
        self._build_panels()
        self._build_log()

        self.refresh_ports()
        self.after(50, self.poll_queue)
        self.after(1000, self.update_stale_indicator)
        self.after(300, self.watch_ports)
        self.after(50, self.animate_commanded_sail)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ----- top: connection controls --------------------------------------- #
    def _build_connection_bar(self):
        bar = ctk.CTkFrame(self, corner_radius=10)
        bar.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 6))
        for i in range(7):
            bar.grid_columnconfigure(i, weight=0)
        bar.grid_columnconfigure(6, weight=1)

        ctk.CTkLabel(bar, text="COM Port:").grid(
            row=0, column=0, padx=(12, 6), pady=10)
        self.port_combo = ctk.CTkComboBox(bar, values=["(no ports)"], width=320)
        self.port_combo.grid(row=0, column=1, padx=6, pady=10)

        self.refresh_btn = ctk.CTkButton(
            bar, text="\u21bb Refresh", width=90, command=self.refresh_ports)
        self.refresh_btn.grid(row=0, column=2, padx=6, pady=10)

        ctk.CTkLabel(bar, text="Baud:").grid(row=0, column=3, padx=(18, 6))
        self.baud_combo = ctk.CTkComboBox(bar, values=BAUD_RATES, width=110)
        self.baud_combo.set("115200")
        self.baud_combo.grid(row=0, column=4, padx=6, pady=10)

        self.connect_btn = ctk.CTkButton(
            bar, text="Connect", width=120, command=self.toggle_connection)
        self.connect_btn.grid(row=0, column=5, padx=6, pady=10)

        self.status_label = ctk.CTkLabel(
            bar, text="\u25cb  Disconnected", text_color="#d05b5b",
            font=ctk.CTkFont(size=14, weight="bold"))
        self.status_label.grid(row=0, column=6, padx=(18, 12), sticky="e")

        self.autoconnect = ctk.CTkCheckBox(
            bar, text="Auto-connect when an ST-Link COM port is plugged in")
        self.autoconnect.select()
        self.autoconnect.grid(row=1, column=0, columnspan=5, padx=12,
                              pady=(0, 10), sticky="w")

        self.view3d_btn = ctk.CTkButton(
            bar, text="3D View & Map", width=130, command=self.open_3d_window)
        self.view3d_btn.grid(row=1, column=5, padx=6, pady=(0, 10), sticky="e")

        self.dark_switch = ctk.CTkSwitch(
            bar, text="Dark Mode", command=self.toggle_appearance)
        self.dark_switch.select()  # app starts in dark mode
        self.dark_switch.grid(row=1, column=6, padx=(6, 12), pady=(0, 10),
                              sticky="e")

    # ----- middle: controller + telemetry --------------------------------- #
    def _build_panels(self):
        wrap = ctk.CTkFrame(self, fg_color="transparent")
        wrap.grid(row=1, column=0, sticky="nsew", padx=12, pady=6)
        wrap.grid_columnconfigure(0, weight=2, uniform="cols")
        wrap.grid_columnconfigure(1, weight=3, uniform="cols")
        wrap.grid_rowconfigure(0, weight=1)

        self._build_controller_card(wrap)
        self._build_telemetry_card(wrap)

    def _build_controller_card(self, parent):
        card = ctk.CTkFrame(parent, corner_radius=10)
        card.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        card.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(card, text="Controller Output",
                     font=ctk.CTkFont(size=16, weight="bold")).grid(
            row=0, column=0, sticky="w", padx=16, pady=(14, 8))

        # Sail
        srow = ctk.CTkFrame(card, fg_color="transparent")
        srow.grid(row=1, column=0, sticky="ew", padx=16, pady=(4, 0))
        srow.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(srow, text="Sail (left stick)",
                     font=ctk.CTkFont(size=13)).grid(row=0, column=0, sticky="w")
        self.sail_value = ctk.CTkLabel(
            srow, text="0", font=ctk.CTkFont(size=22, weight="bold"),
            text_color="#3b8ed0")
        self.sail_value.grid(row=0, column=1, sticky="e")
        self.sail_bar = CenteredBar(card, fill="#3b8ed0")
        self.sail_bar.grid(row=2, column=0, sticky="ew", padx=16, pady=(2, 10))

        # Rudder
        rrow = ctk.CTkFrame(card, fg_color="transparent")
        rrow.grid(row=3, column=0, sticky="ew", padx=16, pady=(4, 0))
        rrow.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(rrow, text="Rudder (right stick)",
                     font=ctk.CTkFont(size=13)).grid(row=0, column=0, sticky="w")
        self.rudder_value = ctk.CTkLabel(
            rrow, text="0", font=ctk.CTkFont(size=22, weight="bold"),
            text_color="#e8a33d")
        self.rudder_value.grid(row=0, column=1, sticky="e")
        self.rudder_bar = CenteredBar(card, fill="#e8a33d")
        self.rudder_bar.grid(row=4, column=0, sticky="ew", padx=16, pady=(2, 14))

        # Packet stats (compact single row)
        stats = ctk.CTkFrame(card, fg_color="transparent")
        stats.grid(row=5, column=0, sticky="ew", padx=16, pady=(0, 6))
        stats.grid_columnconfigure((0, 1), weight=1)
        self.dropped_label = self._stat_inline(stats, 0, "Dropped", "#d0a23b")
        self.overruns_label = self._stat_inline(stats, 1, "Overruns", "#6c9c6c")

        # Boat schematics: commanded (your sticks) vs actual (from the boat)
        boats = ctk.CTkFrame(card, fg_color="transparent")
        boats.grid(row=6, column=0, sticky="nsew", padx=12, pady=(0, 12))
        boats.grid_columnconfigure((0, 1), weight=1, uniform="boats")
        card.grid_rowconfigure(6, weight=1)

        self.boat_cmd = self._build_boat_panel(
            boats, 0, "Commanded", "from your joysticks")
        self.boat_act = self._build_boat_panel(
            boats, 1, "Actual", "reported by the boat")

    def _build_boat_panel(self, parent, col, title, subtitle):
        panel = ctk.CTkFrame(parent, corner_radius=8,
                             fg_color=("#e6e6ec", "#26262e"))
        panel.grid(row=0, column=col, sticky="nsew", padx=4)
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(2, weight=1)
        ctk.CTkLabel(panel, text=title,
                     font=ctk.CTkFont(size=13, weight="bold")).grid(
            row=0, column=0, pady=(8, 0))
        ctk.CTkLabel(panel, text=subtitle, font=ctk.CTkFont(size=10),
                     text_color=("gray45", "gray60")).grid(row=1, column=0)
        bv = BoatView(panel, width=220, height=210)
        bv.grid(row=2, column=0, sticky="nsew", padx=8, pady=(4, 10))
        return bv

    def _stat_inline(self, parent, col, title, color):
        cell = ctk.CTkFrame(parent, fg_color="transparent")
        cell.grid(row=0, column=col, sticky="ew", padx=4, pady=2)
        cell.grid_columnconfigure(0, weight=1)
        cell.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(cell, text=title, font=ctk.CTkFont(size=12),
                     anchor="e").grid(row=0, column=0, sticky="e", padx=(0, 6))
        val = ctk.CTkLabel(cell, text="0",
                           font=ctk.CTkFont(size=15, weight="bold"),
                           text_color=color, anchor="w")
        val.grid(row=0, column=1, sticky="w")
        return val

    def _build_telemetry_card(self, parent):
        card = ctk.CTkFrame(parent, corner_radius=10)
        card.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        card.grid_columnconfigure(0, weight=1)

        head = ctk.CTkFrame(card, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=12, pady=(8, 2))
        head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(head, text="Sailboat Telemetry (XBee RX)",
                     font=ctk.CTkFont(size=13, weight="bold")).grid(
            row=0, column=0, sticky="w")
        self.rx_age_label = ctk.CTkLabel(head, text="no data",
                                         text_color="#888",
                                         font=ctk.CTkFont(size=11))
        self.rx_age_label.grid(row=0, column=1, sticky="e")

        row = 1
        # Two sub-columns: Target/Setpoints on the left, Boat State on the right.
        # This halves the vertical height used by the field list.
        cols_frame = ctk.CTkFrame(card, fg_color="transparent")
        cols_frame.grid(row=row, column=0, sticky="ew", padx=6, pady=(2, 0))
        cols_frame.grid_columnconfigure(0, weight=1, uniform="tcols")
        cols_frame.grid_columnconfigure(1, weight=0)   # divider
        cols_frame.grid_columnconfigure(2, weight=1, uniform="tcols")

        from collections import OrderedDict
        groups = OrderedDict()
        for key, label, group in TELEMETRY_FIELDS:
            groups.setdefault(group, []).append((key, label))

        # vertical divider
        div = tk.Frame(cols_frame, width=1,
                       bg="#3a3a4a" if ctk.get_appearance_mode() == "Dark"
                       else "#c0c0cc")
        div.grid(row=0, column=1, sticky="ns", padx=6)

        for col_idx, (group_name, fields) in enumerate(groups.items()):
            grid_col = 0 if col_idx == 0 else 2   # skip the divider column
            grp = ctk.CTkFrame(cols_frame, fg_color="transparent")
            grp.grid(row=0, column=grid_col, sticky="nsew", padx=(6, 6))
            grp.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(grp, text=group_name,
                         font=ctk.CTkFont(size=10, weight="bold"),
                         text_color="#7a9fce").grid(
                row=0, column=0, columnspan=2, sticky="w", pady=(4, 2))
            for f_idx, (key, label) in enumerate(fields):
                frow = ctk.CTkFrame(grp, fg_color="transparent")
                frow.grid(row=f_idx + 1, column=0, sticky="ew", pady=0)
                frow.grid_columnconfigure(0, weight=1)
                ctk.CTkLabel(frow, text=f"{label}  ({key})",
                             font=ctk.CTkFont(size=11),
                             anchor="w").grid(row=0, column=0, sticky="w")
                val = ctk.CTkLabel(frow, text="\u2014",
                                   font=ctk.CTkFont(size=12, weight="bold"),
                                   anchor="e")
                val.grid(row=0, column=1, sticky="e")
                self.telemetry_labels[key] = val

        row += 1

        # Wind rose + rolling trend, filling the spare space below the fields.
        extra = ctk.CTkFrame(card, fg_color="transparent")
        extra.grid(row=row, column=0, sticky="nsew", padx=10, pady=(8, 10))
        card.grid_rowconfigure(row, weight=1)
        extra.grid_columnconfigure(0, weight=1)
        extra.grid_columnconfigure(1, weight=1)
        extra.grid_rowconfigure(0, weight=1)
        self.wind_rose = WindRose(extra, size=220)
        self.wind_rose.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self.trend = TrendPlot(extra)
        self.trend.grid(row=0, column=1, sticky="nsew")

    # ----- bottom: raw log ------------------------------------------------- #
    def _build_log(self):
        self._log_collapsed = False

        self.log_card = ctk.CTkFrame(self, corner_radius=10)
        self.log_card.grid(row=2, column=0, sticky="nsew", padx=12, pady=(6, 12))
        self.log_card.grid_columnconfigure(0, weight=1)
        self.log_card.grid_rowconfigure(1, weight=1)

        head = ctk.CTkFrame(self.log_card, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=12, pady=(8, 4))
        head.grid_columnconfigure(0, weight=1)

        # collapse/expand button — far left so it's obviously a toggle
        self.log_toggle_btn = ctk.CTkButton(
            head, text="\u25bc", width=28, height=24,
            font=ctk.CTkFont(size=11),
            fg_color="transparent", hover_color=("gray75", "gray30"),
            command=self.toggle_log)
        self.log_toggle_btn.grid(row=0, column=0, sticky="w", padx=(0, 6))

        ctk.CTkLabel(head, text="Raw Serial Log",
                     font=ctk.CTkFont(size=14, weight="bold")).grid(
            row=0, column=1, sticky="w")
        head.grid_columnconfigure(1, weight=1)

        self.autoscroll = ctk.CTkCheckBox(head, text="Auto-scroll")
        self.autoscroll.select()
        self.autoscroll.grid(row=0, column=2, padx=8)
        self.record_btn = ctk.CTkButton(head, text="\u25cf Record", width=92,
                                        command=self.toggle_record)
        self.record_btn.grid(row=0, column=3, padx=4)
        self._rec_default_fg = self.record_btn.cget("fg_color")
        self._rec_default_hover = self.record_btn.cget("hover_color")
        self.export_btn = ctk.CTkButton(head, text="Export GPX", width=92,
                                        command=self.export_gpx)
        self.export_btn.grid(row=0, column=4, padx=4)
        self.record_status = ctk.CTkLabel(head, text="not recording",
                                          text_color=("gray45", "gray60"),
                                          font=ctk.CTkFont(size=11))
        self.record_status.grid(row=0, column=5, padx=(8, 8))
        ctk.CTkButton(head, text="Clear", width=80,
                      command=self.clear_log).grid(row=0, column=6, padx=4)

        self.log = ctk.CTkTextbox(self.log_card, font=ctk.CTkFont(
            family="Consolas", size=12))
        self.log.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))
        self.log.configure(state="disabled")

    def toggle_log(self):
        """Collapse or expand the raw serial log body."""
        self._log_collapsed = not self._log_collapsed
        if self._log_collapsed:
            # hide the textbox, shrink the row to just the header
            self.log.grid_remove()
            self.log_card.grid_rowconfigure(1, weight=0, minsize=0)
            self.grid_rowconfigure(2, weight=0, minsize=0)
            self.log_toggle_btn.configure(text="\u25b2")
        else:
            # restore
            self.log.grid()
            self.log_card.grid_rowconfigure(1, weight=1)
            self.grid_rowconfigure(2, weight=1, minsize=155)
            self.log_toggle_btn.configure(text="\u25bc")


    # ----- port handling --------------------------------------------------- #
    def refresh_ports(self):
        ports = serial.tools.list_ports.comports()
        self.port_map.clear()
        display = []
        for p in ports:
            desc = p.description or "Unknown device"
            label = f"{p.device} \u2014 {desc}"
            self.port_map[label] = p.device
            display.append(label)
        if not display:
            display = ["(no ports found)"]
            self.port_combo.configure(values=display)
            self.port_combo.set(display[0])
        else:
            self.port_combo.configure(values=display)
            # keep current selection if still present, else pick first
            if self.port_combo.get() not in self.port_map:
                self.port_combo.set(display[0])

    # ----- appearance ----------------------------------------------------- #
    def toggle_appearance(self):
        """Switch between light and dark mode.

        CTk widgets re-theme themselves, but the raw-canvas gauges don't, so
        they're refreshed explicitly here.
        """
        mode = "dark" if self.dark_switch.get() else "light"
        ctk.set_appearance_mode(mode)
        self.sail_bar.refresh_theme()
        self.rudder_bar.refresh_theme()
        self.boat_cmd.refresh_theme()
        self.boat_act.refresh_theme()
        self.wind_rose.refresh_theme()
        self.trend.refresh_theme()
        if self.view3d is not None:
            self.view3d.refresh_theme()

    # ----- 3D view + GPS map window --------------------------------------- #
    def open_3d_window(self):
        """Open (or focus) the pseudo-3D boat + GPS map window."""
        if self.win3d is not None and self.win3d.winfo_exists():
            self.win3d.focus()
            return

        try:
            import tkintermapview
        except ImportError:
            self.append_log("** 'tkintermapview' is not installed. Run: "
                            "pip install tkintermapview **")
            tkintermapview = None

        self.win3d = ctk.CTkToplevel(self)
        self.win3d.title("3D View & GPS Map")
        self.win3d.geometry("1000x540")
        self.win3d.grid_columnconfigure(0, weight=1, uniform="v3")
        self.win3d.grid_columnconfigure(1, weight=1, uniform="v3")
        self.win3d.grid_rowconfigure(0, weight=1)
        self.win3d.protocol("WM_DELETE_WINDOW", self._close_3d_window)

        # Left: pseudo-3D boat
        left = ctk.CTkFrame(self.win3d, corner_radius=10)
        left.grid(row=0, column=0, sticky="nsew", padx=(10, 5), pady=10)
        left.grid_columnconfigure(0, weight=1)
        left.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(left, text="Boat (3D)",
                     font=ctk.CTkFont(size=15, weight="bold")).grid(
            row=0, column=0, sticky="w", padx=14, pady=(12, 4))
        self.view3d = Boat3DView(left)
        self.view3d.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 12))

        # Right: GPS map
        right = ctk.CTkFrame(self.win3d, corner_radius=10)
        right.grid(row=0, column=1, sticky="nsew", padx=(5, 10), pady=10)
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(right, text="Position (GPS)",
                     font=ctk.CTkFont(size=15, weight="bold")).grid(
            row=0, column=0, sticky="w", padx=14, pady=(12, 4))
        if tkintermapview is not None:
            self.mapview = tkintermapview.TkinterMapView(right, corner_radius=8)
            self.mapview.grid(row=1, column=0, sticky="nsew", padx=10,
                             pady=(0, 12))
            self.mapview.set_zoom(15)
        else:
            ctk.CTkLabel(right, text="Map unavailable.\nInstall it with:\n"
                         "pip install tkintermapview",
                         justify="center").grid(row=1, column=0, padx=20,
                                                pady=20)
            self.mapview = None

        # Seed with the latest known state
        self._refresh_3d_from_latest()

        # CTkToplevel applies its title bar / icon on a short timer, which can
        # re-stack the new window behind its parent. Raise it just after that.
        self.win3d.after(250, self.win3d.lift)
        self.win3d.after(260, self.win3d.focus)

    def _close_3d_window(self):
        if self.win3d is not None:
            self.win3d.destroy()
        self.win3d = None
        self.view3d = None
        self.mapview = None
        self.boat_marker = None
        self.wp_marker = None

    def _refresh_3d_from_latest(self):
        """Push the most recent telemetry into the 3D view."""
        if self.view3d is None:
            return
        self.view3d.set_state(getattr(self, "_last_heading", 0.0),
                              self.boat_act.sail_angle,
                              self.boat_act.rudder_angle)

    def update_3d_and_map(self, d):
        """Feed the latest state into the 3D boat and the GPS map (if open)."""
        self._last_heading = d.get("cb", getattr(self, "_last_heading", 0.0))
        if self.view3d is not None:
            self.view3d.set_state(self._last_heading,
                                  self.boat_act.sail_angle,
                                  self.boat_act.rudder_angle)
        if self.mapview is None:
            return
        try:
            if self.gps_track:
                lat, lon, _ = self.gps_track[-1]
                if self.boat_marker is None:
                    self.boat_marker = self.mapview.set_marker(lat, lon,
                                                               text="Boat")
                    self.mapview.set_position(lat, lon)
                else:
                    self.boat_marker.set_position(lat, lon)
                coords = [(p[0], p[1]) for p in self.gps_track[-500:]]
                if len(coords) > 1:
                    self.mapview.delete_all_path()
                    self.mapview.set_path(coords)
            tlat = d.get("tlat")
            tlon = d.get("tlon")
            if (tlat is not None and tlon is not None
                    and (abs(tlat) > 1e-4 or abs(tlon) > 1e-4)):
                if self.wp_marker is None:
                    self.wp_marker = self.mapview.set_marker(tlat, tlon,
                                                             text="Waypoint")
                else:
                    self.wp_marker.set_position(tlat, tlon)
        except Exception:
            pass  # window may be tearing down as a packet lands

    # ----- recording: CSV + GPX ------------------------------------------- #
    def toggle_record(self):
        if self.csv_file is None:
            self.start_recording()
        else:
            self.stop_recording()

    def start_recording(self):
        try:
            os.makedirs("logs", exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.csv_path = os.path.join("logs", f"telemetry_{stamp}.csv")
            self.csv_file = open(self.csv_path, "w", newline="")
        except OSError as e:
            self.append_log(f"** Could not start recording: {e} **")
            self.csv_file = None
            return
        self.csv_writer = csv.writer(self.csv_file)
        self.csv_writer.writerow(["timestamp"] +
                                 [k for k, _, _ in TELEMETRY_FIELDS])
        self.csv_rows = 0
        self.record_btn.configure(text="\u25a0 Stop", fg_color="#c0552e",
                                  hover_color="#a8482a")
        self.append_log(f"** Recording to {self.csv_path} **")
        self._update_record_status()

    def stop_recording(self):
        if self.csv_file is not None:
            try:
                self.csv_file.close()
            except Exception:
                pass
        self.append_log(f"** Recording stopped: {self.csv_path} "
                        f"({self.csv_rows} rows) **")
        self.csv_file = None
        self.csv_writer = None
        self.record_btn.configure(text="\u25cf Record",
                                  fg_color=self._rec_default_fg,
                                  hover_color=self._rec_default_hover)
        self._update_record_status()

    def _record_row(self, d):
        if self.csv_writer is None:
            return
        row = [datetime.now().isoformat(timespec="milliseconds")]
        row += [d.get(k, "") for k, _, _ in TELEMETRY_FIELDS]
        try:
            self.csv_writer.writerow(row)
            self.csv_rows += 1
            self._update_record_status()
        except Exception:
            pass

    def _update_record_status(self):
        if self.csv_file is not None:
            self.record_status.configure(
                text=f"\u25cf REC  {self.csv_rows} rows", text_color="#d05b5b")
        else:
            self.record_status.configure(text="not recording",
                                         text_color=("gray45", "gray60"))

    def export_gpx(self):
        if not self.gps_track:
            self.append_log("** No GPS points to export yet. **")
            return
        try:
            os.makedirs("logs", exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = os.path.join("logs", f"track_{stamp}.gpx")
            with open(path, "w", encoding="utf-8") as f:
                f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
                f.write('<gpx version="1.1" creator="Sailboat Ground Station '
                        'Monitor" xmlns="http://www.topografix.com/GPX/1/1">\n')
                f.write(' <trk><name>Sailboat track</name><trkseg>\n')
                for lat, lon, t in self.gps_track:
                    f.write(f'  <trkpt lat="{lat:.7f}" lon="{lon:.7f}">'
                            f'<time>{t.isoformat()}</time></trkpt>\n')
                f.write(' </trkseg></trk>\n</gpx>\n')
            self.append_log(f"** Exported {len(self.gps_track)} points to "
                            f"{path} **")
        except OSError as e:
            self.append_log(f"** GPX export failed: {e} **")

    def watch_ports(self):
        """Periodically scan COM ports; auto-connect to a new ST-Link.

        Runs on the main thread via .after(). The first pass only records a
        baseline of the ports already present, so we do NOT auto-grab a board
        that was already plugged in before the program started -- only devices
        that appear *after* launch (i.e. get plugged in, or unplugged and
        re-plugged) trigger an auto-connect.
        """
        ports = {p.device: p for p in serial.tools.list_ports.comports()}
        devices = set(ports)

        # First run: just take the baseline, don't act.
        if self.known_devices is None:
            self.known_devices = devices
            self.after(PORT_POLL_MS, self.watch_ports)
            return

        added = devices - self.known_devices
        removed = self.known_devices - devices
        self.known_devices = devices

        # Keep the dropdown in sync whenever hardware comes or goes.
        if added or removed:
            self.refresh_ports()

        # A newly appeared ST-Link becomes the device we want to connect to.
        for dev in sorted(added):
            if is_stlink(ports[dev]):
                self.auto_target = dev
                self._auto_fail_logged.discard(dev)
                self.append_log(f"** ST-Link detected on {dev} **")

        # If the target got unplugged before we connected, forget it.
        if self.auto_target and self.auto_target not in devices:
            self.auto_target = None

        # Try to connect (and keep retrying) while we have a target and are idle.
        connected = self.ser is not None and self.ser.is_open
        if self.autoconnect.get() and not connected and self.auto_target:
            ok = self.connect(device=self.auto_target, quiet=True)
            if not ok and self.auto_target not in self._auto_fail_logged:
                self.append_log(
                    f"** Could not open {self.auto_target} yet; retrying... **")
                self._auto_fail_logged.add(self.auto_target)

        self.after(PORT_POLL_MS, self.watch_ports)

    # ----- connect / disconnect ------------------------------------------- #
    def toggle_connection(self):
        if self.ser and self.ser.is_open:
            self.disconnect()
        else:
            self.connect()

    def connect(self, device=None, quiet=False):
        """Open the serial port. Returns True on success.

        device : explicit COM device name (used by the auto-connect watcher).
                 When None, the device is taken from the dropdown selection.
        quiet  : when True, suppress the "could not open" log line so the
                 retrying watcher doesn't spam the log on a busy port.
        """
        if device is None:
            sel = self.port_combo.get()
            device = self.port_map.get(sel)
        if not device:
            if not quiet:
                self.append_log("** No valid COM port selected. Click Refresh. **")
            return False
        try:
            baud = int(self.baud_combo.get())
        except ValueError:
            baud = 115200
        try:
            self.ser = serial.Serial(device, baud, timeout=0.2)
        except (serial.SerialException, OSError) as e:
            if not quiet:
                self.append_log(f"** Could not open {device}: {e} **")
            self.ser = None
            return False

        # reflect the connected device in the dropdown
        for label, dev in self.port_map.items():
            if dev == device:
                self.port_combo.set(label)
                break

        self.stop_event = threading.Event()
        self.reader = SerialReader(self.ser, self.queue, self.stop_event)
        self.reader.start()

        self.connect_btn.configure(text="Disconnect")
        self.status_label.configure(text="\u25cf  Connected", text_color="#4caf72")
        self.port_combo.configure(state="disabled")
        self.baud_combo.configure(state="disabled")
        self.refresh_btn.configure(state="disabled")
        self.append_log(f"** Connected to {device} @ {baud} baud **")
        return True

    def disconnect(self):
        if self.stop_event:
            self.stop_event.set()
        if self.reader and self.reader.is_alive():
            self.reader.join(timeout=1.0)
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None
        self.reader = None
        self.stop_event = None
        # A manual disconnect (or a cable pull) clears the auto-connect target,
        # so the watcher won't immediately grab the same port again. It will
        # only auto-connect after the device is unplugged and plugged back in.
        self.auto_target = None
        # Stop the commanded sail from spinning once data stops arriving.
        self.cmd_sail_input = 0.0

        self.connect_btn.configure(text="Connect")
        self.status_label.configure(text="\u25cb  Disconnected",
                                    text_color="#d05b5b")
        self.port_combo.configure(state="normal")
        self.baud_combo.configure(state="normal")
        self.refresh_btn.configure(state="normal")
        self.append_log("** Disconnected **")

    # ----- queue pump (main thread) --------------------------------------- #
    def poll_queue(self):
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "line":
                    self.handle_line(payload)
                elif kind == "error":
                    self.append_log(f"** {payload} **")
                    self.disconnect()
        except queue.Empty:
            pass
        self.after(50, self.poll_queue)

    def handle_line(self, line: str):
        self.append_log(line)

        sr = parse_sail_rudder(line)
        if sr is not None:
            self.update_controller(sr)
            return

        tel = parse_xbee(line)
        if tel is not None:
            self.update_telemetry(tel)

    def animate_commanded_sail(self):
        """Advance the commanded boat's sail at a constant slew rate.

        Continuous-rotation model: the sail spins at SAIL_ROTATION_RATE_DPS while
        the stick is deflected, with the direction matching the physical boat
        (sign chosen below). Near-zero commands hold position. The rudder is
        positional and applied as-is.
        """
        now = time.monotonic()
        if self._last_sail_tick is None:
            self._last_sail_tick = now
        dt = now - self._last_sail_tick
        self._last_sail_tick = now

        if abs(self.cmd_sail_input) >= SAIL_CMD_DEADBAND:
            direction = 1.0 if self.cmd_sail_input > 0 else -1.0
            self.cmd_sail_angle += direction * SAIL_ROTATION_RATE_DPS * dt
            # wrap into [0, 360)
            self.cmd_sail_angle %= 360.0

        self.boat_cmd.set_angles(self.cmd_sail_angle, self.cmd_rudder)
        self.after(50, self.animate_commanded_sail)

    def update_controller(self, d):
        self.sail_value.configure(text=str(d["sail"]))
        self.sail_bar.set_value(d["sail"])
        self.rudder_value.configure(text=str(d["rudder"]))
        self.rudder_bar.set_value(d["rudder"])
        # The sail command sets a rotation direction, not an angle; the boat
        # drawing is advanced by animate_commanded_sail(). The rudder is
        # positional, so it's applied directly.
        self.cmd_sail_input = float(d["sail"])
        self.cmd_rudder = float(d["rudder"])
        self.dropped_label.configure(text=str(d["dropped"]))
        self.overruns_label.configure(text=str(d["overruns"]))
        # colour the counters if anything is non-zero
        self.dropped_label.configure(
            text_color="#d05b5b" if d["dropped"] else "#d0a23b")
        self.overruns_label.configure(
            text_color="#d05b5b" if d["overruns"] else "#6c9c6c")

    def update_telemetry(self, d):
        for key, val_label in self.telemetry_labels.items():
            if key in d:
                # ("light-mode colour", "dark-mode colour") so values stay
                # readable in both themes; CTk picks and updates automatically.
                val_label.configure(text=fmt_value(key, d[key]),
                                    text_color=("#1f1f1f", "#ffffff"))

        # Drive the boat schematic from the boat's *actual* reported angles:
        #  - sail:   'sa' = AS5600 encoder angle, shown directly (0-360) so the
        #            drawn value matches the 'sa' telemetry field. 0 deg = aft.
        #  - rudder: 'tra' = target rudder angle. The rudder is an open-loop
        #            servo with no feedback, so this commanded value is the
        #            only rudder position the PCB reports.
        sail = self.boat_act.sail_angle
        rudder = self.boat_act.rudder_angle
        if "sa" in d:
            sail = d["sa"] % 360.0
        if "tra" in d:
            rudder = d["tra"]
        self.boat_act.set_angles(sail, rudder)

        # GPS breadcrumb track (used by the map and GPX export). Append before
        # the map update so it can read the newest point. Skip (0,0)/no-fix and
        # duplicate consecutive points.
        lat, lon = d.get("clat"), d.get("clon")
        if (lat is not None and lon is not None
                and (abs(lat) > 1e-4 or abs(lon) > 1e-4)):
            if not self.gps_track or self.gps_track[-1][:2] != (lat, lon):
                self.gps_track.append((lat, lon, datetime.now()))

        # Feed the pseudo-3D boat (heading/sail/rudder) and the GPS map.
        self.update_3d_and_map(d)

        # Wind compass rose + rolling trend plot.
        if "wa" in d:
            self.wind_rose.set_wind(d["wa"])
        self.trend.add_sample(d.get("wa", 0.0), d.get("cb", 0.0),
                              d.get("sa", 0.0))

        # CSV recording (no-op unless recording is active).
        self._record_row(d)

        self.last_rx_time = datetime.now()

    def update_stale_indicator(self):
        if self.last_rx_time is None:
            self.rx_age_label.configure(text="no data", text_color="#888")
        else:
            age = (datetime.now() - self.last_rx_time).total_seconds()
            if age < 2:
                self.rx_age_label.configure(text="live", text_color="#4caf72")
            else:
                self.rx_age_label.configure(
                    text=f"stale ({int(age)}s ago)", text_color="#d0a23b")
        self.after(1000, self.update_stale_indicator)

    # ----- log helpers ----------------------------------------------------- #
    def append_log(self, text: str):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        # trim to keep memory bounded
        line_count = int(self.log.index("end-1c").split(".")[0])
        if line_count > MAX_LOG_LINES:
            self.log.delete("1.0", f"{line_count - MAX_LOG_LINES}.0")
        if self.autoscroll.get():
            self.log.see("end")
        self.log.configure(state="disabled")

    def clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    # ----- shutdown -------------------------------------------------------- #
    def on_close(self):
        if self.csv_file is not None:
            self.stop_recording()
        self.disconnect()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()