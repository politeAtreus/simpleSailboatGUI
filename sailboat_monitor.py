"""Sailboat Ground Station Monitor.

A CustomTkinter GUI for watching the STM32 joystick controller's serial
output. It parses two line types over the COM port:

1. Controller status (printed continuously):
       sail=-19  rudder=0  | dropped=0  overruns=0

2. Telemetry echoed back from the boat over XBee:
       XBee RX: {"tb":0,...,"sa":347}

Run:
    pip install -r requirements.txt
    python sailboat_monitor.py
"""

import queue
import threading
import time
import tkinter as tk
from datetime import datetime

import customtkinter as ctk
import serial
import serial.tools.list_ports

from parsing import (parse_sail_rudder, parse_xbee, fmt_value,
                     TELEMETRY_FIELDS)
from serial_io import is_stlink, SerialReader
from widgets import CenteredBar, BoatView, WindRose, TrendPlot
from boat3d import Boat3DView
from recording import Recorder
from waypoints import (WaypointStore, WaypointPanel, WaypointMapLayer,
                       STATUS_IDLE, STATUS_NEXT, STATUS_ACTIVE, STATUS_SKIPPED)


# --------------------------------------------------------------------------- #
# Main application
# --------------------------------------------------------------------------- #

BAUD_RATES = ["9600", "19200", "38400", "57600", "115200", "230400", "460800"]
MAX_LOG_LINES = 200000
PORT_POLL_MS = 1000  # how often to scan for COM-port hot-plug / unplug

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
        self.breadcrumb_path = None
        self.gps_track = []          # [(lat, lon, datetime)] recorded always
        # CSV recording
        self.recorder = Recorder()
        # Waypoints (shared between main panel and map view)
        self.waypoints = WaypointStore()
        self.wp_map_layer = None     # created with the 3D window
        self._wp_panel_collapsed = False
        self._wp_sash_x = None

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)   # single weighted row: the outer pane

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
        dark = ctk.get_appearance_mode() == "Dark"
        sash_bg = "#1c1c24" if dark else "#d0d0d8"
        sash_kw = dict(sashwidth=6, sashpad=2, sashrelief="flat",
                       bg=sash_bg, bd=0)

        # Outer horizontal pane: left = main content, right = waypoint panel.
        self.outer_pane = tk.PanedWindow(self, orient=tk.HORIZONTAL,
                                         sashcursor="sb_h_double_arrow",
                                         **sash_kw)
        self.outer_pane.grid(row=1, column=0, sticky="nsew", padx=12,
                             pady=(6, 6))

        # Left side: the existing vertical pane (panels on top, log on bottom).
        left_wrap = tk.Frame(self.outer_pane, bg=sash_bg)
        self.vpane = tk.PanedWindow(left_wrap, orient=tk.VERTICAL,
                                    sashcursor="sb_v_double_arrow", **sash_kw)
        self.vpane.pack(fill=tk.BOTH, expand=True)

        # Inner horizontal pane inside vpane: controller | telemetry.
        panels_bg = tk.Frame(self.vpane, bg=sash_bg)
        self.hpane = tk.PanedWindow(panels_bg, orient=tk.HORIZONTAL,
                                    sashcursor="sb_h_double_arrow", **sash_kw)
        self.hpane.pack(fill=tk.BOTH, expand=True)

        ctrl_wrap = tk.Frame(self.hpane, bg=sash_bg)
        ctrl_wrap.grid_columnconfigure(0, weight=1)
        ctrl_wrap.grid_rowconfigure(0, weight=1)
        self._build_controller_card(ctrl_wrap)
        self.hpane.add(ctrl_wrap, minsize=300, stretch="always")

        telem_wrap = tk.Frame(self.hpane, bg=sash_bg)
        telem_wrap.grid_columnconfigure(0, weight=1)
        telem_wrap.grid_rowconfigure(0, weight=1)
        self._build_telemetry_card(telem_wrap)
        self.hpane.add(telem_wrap, minsize=350, stretch="always")

        self.vpane.add(panels_bg, minsize=280, stretch="always")
        self.outer_pane.add(left_wrap, minsize=600, stretch="always")

        # Right side: waypoint panel (with a thin collapse toggle column).
        wp_wrap = tk.Frame(self.outer_pane, bg=sash_bg)
        wp_wrap.grid_columnconfigure(1, weight=1)
        wp_wrap.grid_rowconfigure(0, weight=1)

        # Toggle button sits in a narrow column on the left edge of the panel.
        self.wp_toggle_btn = ctk.CTkButton(
            wp_wrap, text="\u25b6", width=20, height=40,
            font=ctk.CTkFont(size=11),
            fg_color="transparent", hover_color=("gray75", "gray30"),
            command=self.toggle_waypoint_panel)
        self.wp_toggle_btn.grid(row=0, column=0, sticky="ns", padx=(2, 0))

        self.waypoint_panel = WaypointPanel(
            wp_wrap, self.waypoints,
            on_send=self._send_waypoints_to_boat,
            on_log=self.append_log)
        self.waypoint_panel.grid(row=0, column=1, sticky="nsew", padx=(2, 0))
        self.outer_pane.add(wp_wrap, minsize=40, stretch="always")

        # Default sash positions. Run after geometry settles.
        def _place_sashes():
            hw = self.hpane.winfo_width()
            if hw > 1:
                self.hpane.sash_place(0, int(hw * 0.40), 0)
            ow = self.outer_pane.winfo_width()
            if ow > 1:
                # Waypoint panel takes ~22% of width by default.
                self.outer_pane.sash_place(0, int(ow * 0.78), 0)
        self.vpane.after(80, _place_sashes)

    def toggle_waypoint_panel(self):
        """Collapse or expand the waypoint side panel."""
        self._wp_panel_collapsed = not self._wp_panel_collapsed
        if self._wp_panel_collapsed:
            self._wp_sash_x = self.outer_pane.sash_coord(0)[0]
            total = self.outer_pane.winfo_width()
            self.outer_pane.sash_place(0, max(total - 30, 0), 0)
            self.wp_toggle_btn.configure(text="\u25c0")
        else:
            x = self._wp_sash_x
            if x is None:
                x = int(self.outer_pane.winfo_width() * 0.78)
            self.outer_pane.sash_place(0, x, 0)
            self.wp_toggle_btn.configure(text="\u25b6")

    def _send_waypoints_to_boat(self, json_payload):
        """Hook for transmitting the waypoint list to the controller.

        Two-way comms isn't wired yet, so log the payload for now. Replace
        the print with a UART/XBee TX call once that link exists.
        """
        self.append_log(f"** TX waypoints: {json_payload} **")

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
        card.grid(row=0, column=0, sticky="nsew", padx=(3, 0))
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

        # The log card is the bottom pane of the vertical PanedWindow.
        self.log_card = ctk.CTkFrame(self.vpane, corner_radius=10)
        self.log_card.grid_columnconfigure(0, weight=1)
        self.log_card.grid_rowconfigure(1, weight=1)
        self.vpane.add(self.log_card, minsize=46, stretch="always")

        # Set default vertical split after geometry finalises (~72/28).
        def _place_vsash():
            h = self.vpane.winfo_height()
            if h > 1:
                self.vpane.sash_place(0, 0, int(h * 0.72))
        self.vpane.after(100, _place_vsash)

        head = ctk.CTkFrame(self.log_card, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=12, pady=(8, 4))
        head.grid_columnconfigure(0, weight=1)

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
        """Collapse or expand the log by moving the vertical pane sash."""
        self._log_collapsed = not self._log_collapsed
        if self._log_collapsed:
            # Save current sash Y so we can restore it.
            self._log_sash_y = self.vpane.sash_coord(0)[1]
            # Push the sash to leave only the header strip visible (~46 px).
            total = self.vpane.winfo_height()
            self.vpane.sash_place(0, 0, max(total - 46, 0))
            self.log_toggle_btn.configure(text="\u25b2")
        else:
            # Restore previous sash position (fall back to 72% if never set).
            y = getattr(self, "_log_sash_y",
                        int(self.vpane.winfo_height() * 0.72))
            self.vpane.sash_place(0, 0, y)
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
        self.win3d.geometry("1100x600")
        self.win3d.grid_columnconfigure(0, weight=1)
        self.win3d.grid_rowconfigure(0, weight=1)
        self.win3d.protocol("WM_DELETE_WINDOW", self._close_3d_window)

        # PanedWindow gives a draggable sash between the two panels.
        # sashwidth=6 makes it easy to grab; sashcursor shows a resize cursor.
        paned = tk.PanedWindow(
            self.win3d, orient=tk.HORIZONTAL,
            sashwidth=6, sashpad=2, sashcursor="sb_h_double_arrow",
            sashrelief="flat",
            bg="#1c1c24" if ctk.get_appearance_mode() == "Dark" else "#dbdbdb",
            bd=0)
        paned.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)

        # Left: pseudo-3D boat
        left = ctk.CTkFrame(paned, corner_radius=10)
        left.grid_columnconfigure(0, weight=1)
        left.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(left, text="Boat (3D)",
                     font=ctk.CTkFont(size=15, weight="bold")).grid(
            row=0, column=0, sticky="w", padx=14, pady=(12, 4))
        self.view3d = Boat3DView(left)
        self.view3d.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 12))
        paned.add(left, minsize=280, stretch="always")

        # Right: GPS map
        right = ctk.CTkFrame(paned, corner_radius=10)
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

            # Right-click → add waypoint at the clicked map coordinates.
            try:
                self.mapview.add_right_click_menu_command(
                    label="Add waypoint here",
                    command=self._add_waypoint_from_map,
                    pass_coords=True)
            except Exception as e:
                self.append_log(
                    f"** Right-click menu unavailable ({e}); "
                    f"update tkintermapview to add waypoints from the map. **")

            # Attach the waypoint layer; it'll redraw on every store change.
            self.wp_map_layer = WaypointMapLayer(self.mapview, self.waypoints)
        else:
            ctk.CTkLabel(right, text="Map unavailable.\nInstall it with:\n"
                         "pip install tkintermapview",
                         justify="center").grid(row=1, column=0, padx=20,
                                                pady=20)
            self.mapview = None
        paned.add(right, minsize=280, stretch="always")

        # Default sash position: 40% left, 60% right — set after the window
        # is mapped so the geometry is finalised.
        self.win3d.after(50, lambda: paned.sash_place(0, int(paned.winfo_width() * 0.4), 0)
                         if paned.winfo_width() > 1 else None)

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
        self.breadcrumb_path = None
        self.wp_map_layer = None

    def _add_waypoint_from_map(self, coords):
        """Right-click handler on the map: append a new waypoint here."""
        lat, lon = coords
        wp = self.waypoints.add(lat, lon)
        self.append_log(
            f"** Added waypoint {wp['name']} at "
            f"{lat:.5f}, {lon:.5f} **")

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
                    # Only delete OUR breadcrumb path, not the waypoint path.
                    if self.breadcrumb_path is not None:
                        try:
                            self.breadcrumb_path.delete()
                        except Exception:
                            pass
                    self.breadcrumb_path = self.mapview.set_path(coords)
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
        if not self.recorder.active:
            ok, msg = self.recorder.start()
            self.append_log(f"** {msg} **")
            if ok:
                self.record_btn.configure(text="\u25a0 Stop",
                                          fg_color="#c0552e",
                                          hover_color="#a8482a")
        else:
            msg = self.recorder.stop()
            self.append_log(f"** {msg} **")
            self.record_btn.configure(text="\u25cf Record",
                                      fg_color=self._rec_default_fg,
                                      hover_color=self._rec_default_hover)
        self._update_record_status()

    def _record_row(self, d):
        self.recorder.write_row(d)
        self._update_record_status()

    def _update_record_status(self):
        if self.recorder.active:
            self.record_status.configure(text=self.recorder.status_text(),
                                         text_color="#d05b5b")
        else:
            self.record_status.configure(text=self.recorder.status_text(),
                                         text_color=("gray45", "gray60"))

    def export_gpx(self):
        ok, msg = self.recorder.export_gpx(self.gps_track)
        self.append_log(f"** {msg} **")

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
        if self.recorder.active:
            self.append_log(f"** {self.recorder.stop()} **")
        self.disconnect()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
