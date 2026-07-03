"""Serial connection lifecycle and incoming-data handling.

Mixin class: methods live here but run as App methods via multiple
inheritance. Includes the connection bar widgets, port scanning,
connect/disconnect, and the queue pump that dispatches parsed lines.
"""

import queue
import threading
import time
from datetime import datetime

import customtkinter as ctk
import serial
import serial.tools.list_ports

from parsing import parse_sail_rudder, parse_xbee, fmt_value
from serial_io import is_stlink, SerialReader


BAUD_RATES  = ["9600", "19200", "38400", "57600", "115200", "230400", "460800"]
PORT_POLL_MS = 1000  # how often to scan for COM-port hot-plug / unplug

# The sail is a continuous-rotation drive, not a positional servo: a negative
# command spins it anti-clockwise (viewed from above), a positive command spins
# it clockwise, both at a constant slew rate. The COMMANDED boat integrates this
# over real time instead of treating the stick value as an angle.
SAIL_ROTATION_RATE_DPS = 72.0  # degrees per second
SAIL_CMD_DEADBAND      = 2.0   # |command| below this is treated as "stop"


class ConnectionMixin:
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
                    elif kind == "log":
                        self.append_log(payload)
                    elif kind == "map_dl_done":
                        try:
                            self.map_download_btn.configure(
                                state="normal", text="\u2b07 Save Offline")
                        except Exception:
                            pass
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