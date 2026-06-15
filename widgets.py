"""2D canvas widgets: gauges, top-down boat schematic, wind rose, trend plot.

These are raw tkinter Canvas widgets, so they don't follow CTk's light/dark
switch automatically. Each one redraws on refresh_theme() and pulls colors
from theme.py.
"""

import math
import tkinter as tk
from collections import deque

import customtkinter as ctk

from theme import resolve_color, canvas_bg

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
            c = resolve_color(self.master_frame.cget("fg_color"))
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
            c = resolve_color(self.master_frame.cget("fg_color"))
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
        self.configure(bg=canvas_bg(self.master_frame))
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
        self.configure(bg=canvas_bg(self.master_frame))
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
