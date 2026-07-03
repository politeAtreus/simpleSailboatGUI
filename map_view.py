"""3D boat + GPS map window and its handlers.

Mixin class: methods live here but run as App methods via multiple
inheritance, so they still see self.append_log, self.waypoints, etc.
"""

import os
import threading
import tkinter as tk

import customtkinter as ctk

from boat3d import Boat3DView
from waypoints import WaypointMapLayer


# Map defaults
DEFAULT_MAP_LAT   = 44.6488     # Halifax, NS
DEFAULT_MAP_LON   = -63.5752
DEFAULT_MAP_ZOOM  = 13
MAP_CACHE_DIR     = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "map_downloads")
MAP_CACHE_DB      = os.path.join(MAP_CACHE_DIR, "tiles.db")

TILE_SERVERS = {
    "CartoDB Voyager": "https://a.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}.png",
    "OpenStreetMap":   "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    "CartoDB Light":   "https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
    "CartoDB Dark":    "https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png",
}


class MapViewMixin:
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
            right.grid_rowconfigure(2, weight=1)
    
            map_head = ctk.CTkFrame(right, fg_color="transparent")
            map_head.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 4))
            map_head.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(map_head, text="Position (GPS)",
                         font=ctk.CTkFont(size=15, weight="bold")).grid(
                row=0, column=0, sticky="w")
    
            # Tile server picker — lighter servers load faster and use less bandwidth.
            self.tile_combo = ctk.CTkComboBox(
                map_head, values=list(TILE_SERVERS.keys()), width=160,
                command=self._change_tile_server)
            self.tile_combo.set("CartoDB Voyager")
            self.tile_combo.grid(row=0, column=1, sticky="e", padx=(0, 6))
    
            # Refresh button — re-draws waypoint markers and lines. Useful if
            # the map state gets out of sync after rapid waypoint edits.
            self.map_refresh_btn = ctk.CTkButton(
                map_head, text="\u21bb", width=32,
                font=ctk.CTkFont(size=14, weight="bold"),
                command=self._refresh_map)
            self.map_refresh_btn.grid(row=0, column=2, sticky="e", padx=(0, 6))
    
            # Download tiles — grabs the visible area at current zoom \u00b1 2
            # levels into the on-disk database so it loads offline next time.
            self.map_download_btn = ctk.CTkButton(
                map_head, text="\u2b07 Save Offline", width=110,
                font=ctk.CTkFont(size=12),
                command=self._download_visible_tiles)
            self.map_download_btn.grid(row=0, column=3, sticky="e")
    
            if tkintermapview is not None:
                default_server = TILE_SERVERS["CartoDB Voyager"]
                # Ensure the cache directory exists next to the script.
                try:
                    os.makedirs(MAP_CACHE_DIR, exist_ok=True)
                except OSError as e:
                    self.append_log(f"** Could not create map cache dir: {e} **")
    
                # database_path enables permanent tile caching — tiles download
                # once, then load from the local SQLite DB on future runs.
                self.mapview = tkintermapview.TkinterMapView(
                    right, corner_radius=8, database_path=MAP_CACHE_DB)
                self.mapview.set_tile_server(default_server)
                self.mapview.grid(row=2, column=0, sticky="nsew", padx=10,
                                 pady=(0, 12))
                # Start on Halifax by default. Boat GPS will move the view once
                # a valid fix comes in.
                self.mapview.set_position(DEFAULT_MAP_LAT, DEFAULT_MAP_LON)
                self.mapview.set_zoom(DEFAULT_MAP_ZOOM)
    
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
    
                # Attach the waypoint layer; it handles markers, connecting lines,
                # and the "Adjust nearest waypoint" right-click option.
                self.wp_map_layer = WaypointMapLayer(
                    self.mapview, self.waypoints, on_log=self.append_log)
            else:
                ctk.CTkLabel(right, text="Map unavailable.\nInstall it with:\n"
                             "pip install tkintermapview",
                             justify="center").grid(row=2, column=0, padx=20,
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
    
        def _change_tile_server(self, choice):
            """Switch the map tile server."""
            if self.mapview is None:
                return
            url = TILE_SERVERS.get(choice)
            if url:
                try:
                    self.mapview.set_tile_server(url)
                except Exception:
                    pass
    
        def _refresh_map(self):
            """Re-draw waypoint markers and connecting lines from the store."""
            if self.wp_map_layer is not None:
                self.wp_map_layer.refresh(structural=True)
                self.append_log("** Map waypoints refreshed. **")
    
        def _download_visible_tiles(self):
            """Save the currently visible area to the offline tile database.
    
            Grabs the current zoom \u00b1 2 levels so panning and small zoom
            changes still work offline. Runs in a background thread so the UI
            stays responsive.
            """
            if self.mapview is None:
                return
            try:
                import tkintermapview
            except ImportError:
                return
    
            # Bounding box of the currently visible map area.
            try:
                top_left     = self.mapview.convert_canvas_coords_to_decimal_coords(
                    0, 0)
                bottom_right = self.mapview.convert_canvas_coords_to_decimal_coords(
                    self.mapview.winfo_width(), self.mapview.winfo_height())
            except Exception as e:
                self.append_log(f"** Could not read map bounds: {e} **")
                return
    
            current_zoom = int(self.mapview.zoom)
            zoom_min = max(0,  current_zoom - 4)
            zoom_max = min(19, current_zoom + 4)
    
            # Rough estimate of tile count so the user knows what they're asking for.
            lat_span = abs(top_left[0] - bottom_right[0])
            lon_span = abs(top_left[1] - bottom_right[1])
            est_tiles = 0
            for z in range(zoom_min, zoom_max + 1):
                n = 2 ** z
                est_tiles += max(1, int(n * lon_span / 360.0)) * \
                             max(1, int(n * lat_span / 180.0))
    
            server_name = self.tile_combo.get()
            server_url  = TILE_SERVERS.get(server_name)
    
            self.append_log(
                f"** Downloading tiles for zoom {zoom_min}\u2013{zoom_max} "
                f"(~{est_tiles} tiles) from {server_name}... **")
            self.map_download_btn.configure(state="disabled",
                                            text="Downloading...")
    
            def _worker():
                try:
                    os.makedirs(MAP_CACHE_DIR, exist_ok=True)
                    loader = tkintermapview.OfflineLoader(
                        path=MAP_CACHE_DB, tile_server=server_url)
                    loader.save_offline_tiles(top_left, bottom_right,
                                              zoom_min, zoom_max)
                    self.queue.put(("log", "** Tile download complete. **"))
                except Exception as ex:
                    self.queue.put(("log", f"** Tile download failed: {ex} **"))
                finally:
                    self.queue.put(("map_dl_done", None))
    
            threading.Thread(target=_worker, daemon=True).start()
    
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