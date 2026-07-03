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
import tkinter as tk

import customtkinter as ctk

from widgets import CenteredBar, BoatView, WindRose, TrendPlot
from parsing import TELEMETRY_FIELDS
from recording import Recorder
from waypoints import WaypointStore, WaypointPanel
from connection import ConnectionMixin, BAUD_RATES
from map_view import MapViewMixin


# --------------------------------------------------------------------------- #
# Main application
# --------------------------------------------------------------------------- #

MAX_LOG_LINES = 200000

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")


class App(ConnectionMixin, MapViewMixin, ctk.CTk):
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
