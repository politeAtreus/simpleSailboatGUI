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

import math
import queue
import re
import threading
import time
import tkinter as tk
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
        mast_x, mast_y = cx, cy
        boom_len = L * 0.85
        tip = self._rotate(mast_x, mast_y + boom_len, mast_x, mast_y,
                           self.sail_angle)
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

# Encoder reading (telemetry 'sa') that corresponds to the sail being centred.
# MUST match SAIL_CENTER_DEG in the boat firmware (motor_control.c) so the
# drawn sail deflection lines up with the physical sail.
SAIL_CENTER_DEG = 180.0

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

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

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
        self.autoconnect.grid(row=1, column=0, columnspan=6, padx=12,
                              pady=(0, 10), sticky="w")

        self.dark_switch = ctk.CTkSwitch(
            bar, text="Dark Mode", command=self.toggle_appearance)
        self.dark_switch.select()  # app starts in dark mode
        self.dark_switch.grid(row=1, column=6, padx=(6, 12), pady=(0, 10),
                              sticky="e")

    # ----- middle: controller + telemetry --------------------------------- #
    def _build_panels(self):
        wrap = ctk.CTkFrame(self, fg_color="transparent")
        wrap.grid(row=1, column=0, sticky="ew", padx=12, pady=6)
        wrap.grid_columnconfigure(0, weight=1, uniform="cols")
        wrap.grid_columnconfigure(1, weight=1, uniform="cols")

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
        head.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 4))
        head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(head, text="Sailboat Telemetry (XBee RX)",
                     font=ctk.CTkFont(size=16, weight="bold")).grid(
            row=0, column=0, sticky="w")
        self.rx_age_label = ctk.CTkLabel(head, text="no data",
                                         text_color="#888",
                                         font=ctk.CTkFont(size=12))
        self.rx_age_label.grid(row=0, column=1, sticky="e")

        row = 1
        current_group = None
        for key, label, group in TELEMETRY_FIELDS:
            if group != current_group:
                current_group = group
                ctk.CTkLabel(card, text=group,
                             font=ctk.CTkFont(size=12, weight="bold"),
                             text_color="#7a9fce").grid(
                    row=row, column=0, sticky="w", padx=16, pady=(10, 2))
                row += 1
            field = ctk.CTkFrame(card, fg_color="transparent")
            field.grid(row=row, column=0, sticky="ew", padx=16, pady=1)
            field.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(field, text=f"{label}  ({key})",
                         font=ctk.CTkFont(size=13),
                         anchor="w").grid(row=0, column=0, sticky="w")
            val = ctk.CTkLabel(field, text="\u2014",
                               font=ctk.CTkFont(size=15, weight="bold"),
                               anchor="e")
            val.grid(row=0, column=1, sticky="e")
            self.telemetry_labels[key] = val
            row += 1

    # ----- bottom: raw log ------------------------------------------------- #
    def _build_log(self):
        card = ctk.CTkFrame(self, corner_radius=10)
        card.grid(row=2, column=0, sticky="nsew", padx=12, pady=(6, 12))
        card.grid_columnconfigure(0, weight=1)
        card.grid_rowconfigure(1, weight=1)

        head = ctk.CTkFrame(card, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 4))
        head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(head, text="Raw Serial Log",
                     font=ctk.CTkFont(size=14, weight="bold")).grid(
            row=0, column=0, sticky="w")
        self.autoscroll = ctk.CTkCheckBox(head, text="Auto-scroll")
        self.autoscroll.select()
        self.autoscroll.grid(row=0, column=1, padx=8)
        ctk.CTkButton(head, text="Clear", width=80,
                      command=self.clear_log).grid(row=0, column=2, padx=4)

        self.log = ctk.CTkTextbox(card, font=ctk.CTkFont(
            family="Consolas", size=12))
        self.log.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))
        self.log.configure(state="disabled")

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

    # ----- COM-port hot-plug watcher -------------------------------------- #
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

        Continuous-rotation model: a positive command spins the sail clockwise
        (viewed from above), a negative command anti-clockwise, both at
        SAIL_ROTATION_RATE_DPS. In BoatView's frame, positive angle = starboard,
        so clockwise (toward port) means the angle decreases -> rate = -sign(cmd).
        The rudder is positional and applied as-is.
        """
        now = time.monotonic()
        if self._last_sail_tick is None:
            self._last_sail_tick = now
        dt = now - self._last_sail_tick
        self._last_sail_tick = now

        if abs(self.cmd_sail_input) >= SAIL_CMD_DEADBAND:
            direction = -1.0 if self.cmd_sail_input > 0 else 1.0
            self.cmd_sail_angle += direction * SAIL_ROTATION_RATE_DPS * dt
            # wrap into [-180, 180)
            self.cmd_sail_angle = ((self.cmd_sail_angle + 180.0) % 360.0) - 180.0

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
        #  - sail:   'sa' = AS5600 encoder (0-360), converted to deflection
        #            about SAIL_CENTER_DEG and wrapped into [-180, 180).
        #  - rudder: 'tra' = target rudder angle. The rudder is an open-loop
        #            servo with no feedback, so this commanded value is the
        #            only rudder position the PCB reports.
        sail = self.boat_act.sail_angle
        rudder = self.boat_act.rudder_angle
        if "sa" in d:
            sail = ((d["sa"] - SAIL_CENTER_DEG + 180.0) % 360.0) - 180.0
        if "tra" in d:
            rudder = d["tra"]
        self.boat_act.set_angles(sail, rudder)

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
        self.disconnect()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()