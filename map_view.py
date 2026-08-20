"""3D boat + GPS map window and its handlers.

Mixin class: methods live here but run as App methods via multiple
inheritance, so they still see self.append_log, self.waypoints, etc.

The map uses a small TkinterMapView subclass that fixes two important cache
limitations in tkintermapview 1.29:

1. Tiles fetched during normal interactive use are written to the SQLite
   cache, instead of only being cached in RAM.
2. Base-map and OpenSeaMap overlay tiles are cached separately. Toggling the
   nautical layer therefore recomposites cached tiles instead of downloading
   the base map again.

It also disables TkinterMapView's aggressive radius-8 background pre-cache.
Only tiles actually needed by the visible map are requested, which cuts a
large amount of unnecessary network traffic when panning/zooming.
"""

import io
import math
import os
import re
import sqlite3
import threading
import time
import tkinter as tk
from email.utils import parsedate_to_datetime

import customtkinter as ctk

from boat3d import Boat3DView
from waypoints import WaypointMapLayer


# Map defaults
DEFAULT_MAP_LAT   = 44.6488     # Halifax, NS
DEFAULT_MAP_LON   = -63.5752
DEFAULT_MAP_ZOOM  = 12
MAP_CACHE_DIR     = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "map_downloads")
MAP_CACHE_DB      = os.path.join(MAP_CACHE_DIR, "tiles.db")

OPENSEAMAP_TILE_SERVER = "https://tiles.openseamap.org/seamark/{z}/{x}/{y}.png"
OPENSEAMAP_MAX_ZOOM = 18

# Normal viewed tiles are cached persistently. Seven days is also the minimum
# fallback cache lifetime requested by the public OpenStreetMap tile policy
# when no useful HTTP cache header is supplied.
VIEW_TILE_FALLBACK_TTL_S = 7 * 24 * 60 * 60

# Identify this application instead of using tkintermapview's generic UA.
MAP_USER_AGENT = "SailboatGroundStationMonitor/1.0 (TkinterMapView)"

TILE_SERVERS = {
    "CartoDB Voyager": "https://a.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}.png",
    "OpenStreetMap":   "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    "CartoDB Light":   "https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
    "CartoDB Dark":    "https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png",
}


def _prepare_tile_cache_database(path):
    """Create/tune the shared SQLite tile cache before map threads start."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA cache_size=-32768")       # ~32 MiB SQLite page cache
        conn.execute("PRAGMA busy_timeout=5000")

        # Same core schema used by tkintermapview. Keeping it compatible means
        # OfflineLoader and the normal map can share one database.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS server (
                url VARCHAR(300) PRIMARY KEY NOT NULL,
                max_zoom INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tiles (
                zoom INTEGER NOT NULL,
                x INTEGER NOT NULL,
                y INTEGER NOT NULL,
                server VARCHAR(300) NOT NULL,
                tile_image BLOB NOT NULL,
                CONSTRAINT fk_server FOREIGN KEY (server) REFERENCES server (url),
                CONSTRAINT pk_tiles PRIMARY KEY (zoom, x, y, server)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sections (
                position_a VARCHAR(100) NOT NULL,
                position_b VARCHAR(100) NOT NULL,
                zoom_a INTEGER NOT NULL,
                zoom_b INTEGER NOT NULL,
                server VARCHAR(300) NOT NULL,
                CONSTRAINT fk_server FOREIGN KEY (server) REFERENCES server (url),
                CONSTRAINT pk_sections
                    PRIMARY KEY (position_a, position_b, zoom_a, zoom_b, server)
            )
        """)

        # Extra metadata used only by this application. Tiles inserted by
        # tkintermapview's OfflineLoader have no row here and are therefore
        # treated as intentionally-offline/permanent tiles.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tile_cache_meta (
                zoom INTEGER NOT NULL,
                x INTEGER NOT NULL,
                y INTEGER NOT NULL,
                server VARCHAR(300) NOT NULL,
                cached_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                etag TEXT,
                last_modified TEXT,
                CONSTRAINT pk_tile_cache_meta PRIMARY KEY (zoom, x, y, server)
            )
        """)
        conn.commit()
    finally:
        conn.close()


def _make_cached_mapview_class(tkintermapview):
    """Return a TkinterMapView subclass with persistent base/overlay caching."""
    import requests
    from PIL import Image, ImageTk, UnidentifiedImageError

    # Pillow 10+ removed Image.ANTIALIAS, while tkintermapview 1.29 still
    # references it in its stock overlay path. Keep the alias for any library
    # code that still reaches that path.
    if not hasattr(Image, "ANTIALIAS") and hasattr(Image, "Resampling"):
        Image.ANTIALIAS = Image.Resampling.LANCZOS

    class CachedOverlayMapView(tkintermapview.TkinterMapView):
        """TkinterMapView with a real disk cache for base and overlay tiles."""

        _MEMORY_TILE_LIMIT = 2000
        _NETWORK_RETRY_DELAY_S = 60

        def __init__(self, *args, overlay_max_zoom=OPENSEAMAP_MAX_ZOOM, **kwargs):
            database_path = kwargs.get("database_path")
            if database_path:
                _prepare_tile_cache_database(database_path)

            self.overlay_max_zoom = int(overlay_max_zoom)
            self._http_local = threading.local()
            self._memory_cache_lock = threading.Lock()
            self._failed_tile_until = {}
            # TkinterMapView's constructor briefly points at Berlin and queues
            # tiles before the caller can set the real initial position. Suppress
            # those startup network requests; the Halifax redraw below will queue
            # the tiles that are actually needed.
            self._initializing_map = True
            super().__init__(*args, **kwargs)
            self._initializing_map = False
            self.image_load_queue_tasks = []
            self.image_load_queue_results = []

        # ------------------------------------------------------------------ #
        # Cache identity / redraw
        # ------------------------------------------------------------------ #

        def _overlay_for_zoom(self, zoom):
            if self.overlay_tile_server is None:
                return None
            if int(zoom) > self.overlay_max_zoom:
                return None
            return self.overlay_tile_server

        def _image_cache_key(self, zoom, x, y, tile_server=None,
                             overlay_server="__CURRENT__"):
            base = self.tile_server if tile_server is None else tile_server
            if overlay_server == "__CURRENT__":
                overlay = self._overlay_for_zoom(zoom)
            else:
                overlay = overlay_server
            return (base, overlay, int(zoom), int(x), int(y))

        def get_tile_image_from_cache(self, zoom, x, y):
            key = self._image_cache_key(zoom, x, y)
            with self._memory_cache_lock:
                return self.tile_image_cache.get(key, False)

        def _put_memory_tile(self, key, image_tk):
            with self._memory_cache_lock:
                self.tile_image_cache[key] = image_tk
                excess = len(self.tile_image_cache) - self._MEMORY_TILE_LIMIT
                if excess > 0:
                    # dicts preserve insertion order; discard the oldest images.
                    for old_key in list(self.tile_image_cache.keys())[:excess]:
                        self.tile_image_cache.pop(old_key, None)

        def _redraw_tiles_for_source_change(self):
            """Redraw visible tiles without throwing away other cache variants."""
            self.image_load_queue_tasks = []
            self.image_load_queue_results = []
            self.canvas.delete("tile")
            self.draw_initial_array()
            self.manage_z_order()

        def set_tile_server(self, tile_server: str, tile_size: int = 256,
                            max_zoom: int = 19):
            """Change base source but retain cached tiles from all sources."""
            changed = (tile_server != self.tile_server or
                       tile_size != self.tile_size or
                       max_zoom != self.max_zoom)
            self.max_zoom = max_zoom
            self.tile_size = tile_size
            self.min_zoom = math.ceil(
                math.log2(math.ceil(self.width / self.tile_size)))
            self.tile_server = tile_server
            if changed:
                self._redraw_tiles_for_source_change()

        def set_overlay_tile_server(self, overlay_server):
            """Toggle/change overlay without flushing base or overlay caches."""
            if overlay_server == self.overlay_tile_server:
                return
            self.overlay_tile_server = overlay_server
            self._redraw_tiles_for_source_change()

        # ------------------------------------------------------------------ #
        # Network + SQLite helpers
        # ------------------------------------------------------------------ #

        def _session(self):
            session = getattr(self._http_local, "session", None)
            if session is None:
                session = requests.Session()
                session.headers.update({"User-Agent": MAP_USER_AGENT})
                self._http_local.session = session
            return session

        @staticmethod
        def _tile_url(server, zoom, x, y):
            return (server.replace("{x}", str(x))
                          .replace("{y}", str(y))
                          .replace("{z}", str(zoom)))

        @staticmethod
        def _expires_from_headers(headers, now):
            cache_control = headers.get("Cache-Control", "")
            m = re.search(r"(?:^|,)\s*max-age\s*=\s*(\d+)", cache_control,
                          flags=re.IGNORECASE)
            if m:
                return now + max(0, int(m.group(1)))

            expires = headers.get("Expires")
            if expires:
                try:
                    dt = parsedate_to_datetime(expires)
                    if dt.tzinfo is not None:
                        return max(now, dt.timestamp())
                except Exception:
                    pass

            return now + VIEW_TILE_FALLBACK_TTL_S

        @staticmethod
        def _configure_thread_db(db_cursor):
            if db_cursor is None:
                return
            try:
                db_cursor.execute("PRAGMA busy_timeout=5000")
            except Exception:
                pass

        def _read_disk_tile(self, db_cursor, server, zoom, x, y):
            if db_cursor is None:
                return None, None
            try:
                self._configure_thread_db(db_cursor)
                db_cursor.execute(
                    "SELECT tile_image FROM tiles "
                    "WHERE zoom=? AND x=? AND y=? AND server=?",
                    (zoom, x, y, server))
                row = db_cursor.fetchone()
                if row is None:
                    return None, None

                db_cursor.execute(
                    "SELECT cached_at, expires_at, etag, last_modified "
                    "FROM tile_cache_meta "
                    "WHERE zoom=? AND x=? AND y=? AND server=?",
                    (zoom, x, y, server))
                meta = db_cursor.fetchone()
                return row[0], meta
            except sqlite3.OperationalError:
                return None, None
            except Exception:
                return None, None

        def _write_disk_tile(self, db_cursor, server, zoom, x, y,
                             image_bytes, response_headers):
            if db_cursor is None or image_bytes is None:
                return
            now = time.time()
            expires_at = self._expires_from_headers(response_headers, now)
            try:
                self._configure_thread_db(db_cursor)
                db_cursor.execute(
                    "INSERT OR IGNORE INTO server (url, max_zoom) VALUES (?, ?)",
                    (server, self.max_zoom))
                db_cursor.execute(
                    "INSERT OR REPLACE INTO tiles "
                    "(zoom, x, y, server, tile_image) VALUES (?, ?, ?, ?, ?)",
                    (zoom, x, y, server, sqlite3.Binary(image_bytes)))
                db_cursor.execute(
                    "INSERT OR REPLACE INTO tile_cache_meta "
                    "(zoom, x, y, server, cached_at, expires_at, etag, last_modified) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (zoom, x, y, server, now, expires_at,
                     response_headers.get("ETag"),
                     response_headers.get("Last-Modified")))
                db_cursor.connection.commit()
            except sqlite3.OperationalError:
                # A temporary writer collision must not make the map disappear.
                try:
                    db_cursor.connection.rollback()
                except Exception:
                    pass
            except Exception:
                try:
                    db_cursor.connection.rollback()
                except Exception:
                    pass

        def _refresh_disk_meta(self, db_cursor, server, zoom, x, y,
                               old_meta, response_headers):
            if db_cursor is None:
                return
            now = time.time()
            expires_at = self._expires_from_headers(response_headers, now)
            old_etag = old_meta[2] if old_meta else None
            old_last_modified = old_meta[3] if old_meta else None
            try:
                db_cursor.execute(
                    "INSERT OR REPLACE INTO tile_cache_meta "
                    "(zoom, x, y, server, cached_at, expires_at, etag, last_modified) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (zoom, x, y, server, now, expires_at,
                     response_headers.get("ETag") or old_etag,
                     response_headers.get("Last-Modified") or old_last_modified))
                db_cursor.connection.commit()
            except Exception:
                try:
                    db_cursor.connection.rollback()
                except Exception:
                    pass

        @staticmethod
        def _decode_image(image_bytes):
            if image_bytes is None:
                return None
            with Image.open(io.BytesIO(image_bytes)) as img:
                img.load()
                return img.copy()

        def _load_source_image(self, server, zoom, x, y, db_cursor):
            """Return one source tile, preferring disk and refreshing if stale."""
            cached_bytes, meta = self._read_disk_tile(
                db_cursor, server, zoom, x, y)

            now = time.time()
            # No metadata means the tile came from OfflineLoader or another
            # explicit offline source; keep using it without an online refresh.
            if cached_bytes is not None and meta is None:
                try:
                    return self._decode_image(cached_bytes)
                except Exception:
                    cached_bytes = None

            # Runtime-cached tile still inside its HTTP/fallback lifetime.
            if cached_bytes is not None and meta is not None and now < meta[1]:
                try:
                    return self._decode_image(cached_bytes)
                except Exception:
                    cached_bytes = None

            fail_key = (server, int(zoom), int(x), int(y))
            if self._failed_tile_until.get(fail_key, 0) > now:
                if cached_bytes is not None:
                    try:
                        return self._decode_image(cached_bytes)
                    except Exception:
                        pass
                return None

            headers = {}
            if cached_bytes is not None and meta is not None:
                if meta[2]:
                    headers["If-None-Match"] = meta[2]
                if meta[3]:
                    headers["If-Modified-Since"] = meta[3]

            try:
                response = self._session().get(
                    self._tile_url(server, zoom, x, y),
                    headers=headers, timeout=(3.5, 10))

                if response.status_code == 304 and cached_bytes is not None:
                    self._refresh_disk_meta(
                        db_cursor, server, zoom, x, y, meta, response.headers)
                    self._failed_tile_until.pop(fail_key, None)
                    return self._decode_image(cached_bytes)

                if response.status_code == 404:
                    self._failed_tile_until[fail_key] = (
                        now + self._NETWORK_RETRY_DELAY_S)
                    return None

                response.raise_for_status()
                image_bytes = response.content

                # Validate before committing bad HTML/error bodies into SQLite.
                image = self._decode_image(image_bytes)
                self._write_disk_tile(
                    db_cursor, server, zoom, x, y, image_bytes, response.headers)
                self._failed_tile_until.pop(fail_key, None)
                return image

            except (requests.RequestException, UnidentifiedImageError,
                    OSError, ValueError):
                self._failed_tile_until[fail_key] = (
                    now + self._NETWORK_RETRY_DELAY_S)
                # A stale cached tile is much better than a blank map while the
                # network/server is temporarily unavailable.
                if cached_bytes is not None:
                    try:
                        return self._decode_image(cached_bytes)
                    except Exception:
                        pass
                return None

        # ------------------------------------------------------------------ #
        # TkinterMapView image request override
        # ------------------------------------------------------------------ #

        def request_image(self, zoom: int, x: int, y: int, db_cursor=None):
            if getattr(self, "_initializing_map", False):
                return self.empty_tile_image

            # Capture sources at the start so a switch toggle halfway through a
            # network request cannot store the composite under the wrong key.
            base_server = self.tile_server
            overlay_server = self._overlay_for_zoom(zoom)
            cache_key = self._image_cache_key(
                zoom, x, y, tile_server=base_server,
                overlay_server=overlay_server)

            with self._memory_cache_lock:
                cached_photo = self.tile_image_cache.get(cache_key)
            if cached_photo is not None:
                return cached_photo

            base = self._load_source_image(
                base_server, zoom, x, y, db_cursor)
            if base is None:
                if self.use_database_only:
                    return self.empty_tile_image
                return self.empty_tile_image

            if overlay_server is not None:
                overlay = self._load_source_image(
                    overlay_server, zoom, x, y, db_cursor)
                if overlay is not None:
                    base = base.convert("RGBA")
                    overlay = overlay.convert("RGBA")
                    if overlay.size != (self.tile_size, self.tile_size):
                        if hasattr(Image, "Resampling"):
                            resample = Image.Resampling.LANCZOS
                        else:
                            resample = Image.LANCZOS
                        overlay = overlay.resize(
                            (self.tile_size, self.tile_size), resample)
                    base.alpha_composite(overlay, (0, 0))

            if not self.running:
                return self.empty_tile_image

            image_tk = ImageTk.PhotoImage(base)
            self._put_memory_tile(cache_key, image_tk)
            return image_tk

        def pre_cache(self):
            """Disable tkintermapview's radius-8 speculative downloader.

            The stock implementation can request hundreds of tiles around every
            new centre point. Visible tiles already load in the normal worker
            pool, and every viewed tile is now persisted to SQLite, so that
            aggressive look-ahead is unnecessary and substantially slows the
            nautical overlay.
            """
            while self.running:
                time.sleep(0.25)

    return CachedOverlayMapView


class MapViewMixin:
        # ----- 3D view + GPS map window --------------------------------------- #
        def open_3d_window(self):
            """Open (or focus) the pseudo-3D boat + GPS map window."""
            if self.win3d is not None and self.win3d.winfo_exists():
                self.win3d.focus()
                return

            try:
                import tkintermapview
                CachedOverlayMapView = _make_cached_mapview_class(tkintermapview)
            except ImportError:
                self.append_log("** 'tkintermapview' is not installed. Run: "
                                "pip install tkintermapview **")
                tkintermapview = None
                CachedOverlayMapView = None

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
            self.tile_combo.set("OpenStreetMap")
            self.tile_combo.grid(row=0, column=1, sticky="e", padx=(0, 8))

            # OpenSeaMap seamarks are a transparent XYZ overlay. Keep this as a
            # separate switch so the base map can still be changed independently.
            self.nautical_switch = ctk.CTkSwitch(
                map_head, text="Nautical", width=92,
                command=self._toggle_nautical_overlay)
            self.nautical_switch.select()
            self.nautical_switch.grid(row=0, column=2, sticky="e", padx=(0, 8))

            # Refresh button — re-draws waypoint markers and lines. Useful if
            # the map state gets out of sync after rapid waypoint edits.
            self.map_refresh_btn = ctk.CTkButton(
                map_head, text="\u21bb", width=32,
                font=ctk.CTkFont(size=14, weight="bold"),
                command=self._refresh_map)
            self.map_refresh_btn.grid(row=0, column=3, sticky="e", padx=(0, 6))

            # Download tiles — grabs the visible area at current zoom ± 3
            # levels into the on-disk database so it loads offline next time.
            self.map_download_btn = ctk.CTkButton(
                map_head, text="\u2b07 Save Offline", width=110,
                font=ctk.CTkFont(size=12),
                command=self._download_visible_tiles)
            self.map_download_btn.grid(row=0, column=4, sticky="e")

            if tkintermapview is not None:
                default_server = TILE_SERVERS[self.tile_combo.get()]
                try:
                    _prepare_tile_cache_database(MAP_CACHE_DB)
                except OSError as e:
                    self.append_log(f"** Could not create map cache dir: {e} **")
                except sqlite3.Error as e:
                    self.append_log(f"** Could not initialise map cache DB: {e} **")

                # CachedOverlayMapView keeps base and seamark tiles as separate
                # rows in tiles.db, so toggling Nautical never has to redownload
                # the underlying map.
                self.mapview = CachedOverlayMapView(
                    right, corner_radius=8, database_path=MAP_CACHE_DB,
                    overlay_max_zoom=OPENSEAMAP_MAX_ZOOM)
                self.mapview.set_tile_server(default_server, max_zoom=19)
                self.mapview.set_overlay_tile_server(OPENSEAMAP_TILE_SERVER)
                self.mapview.grid(row=2, column=0, sticky="nsew", padx=10,
                                  pady=(0, 4))

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

                self.nautical_attribution = ctk.CTkLabel(
                    right,
                    text="© OpenStreetMap contributors   |   Nautical: OpenSeaMap",
                    font=ctk.CTkFont(size=9),
                    text_color=("gray45", "gray60"),
                    anchor="e")
                self.nautical_attribution.grid(
                    row=3, column=0, sticky="e", padx=12, pady=(0, 6))
            else:
                ctk.CTkLabel(right, text="Map unavailable.\nInstall it with:\n"
                             "pip install tkintermapview",
                             justify="center").grid(row=2, column=0, padx=20,
                                                    pady=20)
                self.mapview = None
            paned.add(right, minsize=280, stretch="always")

            # Default sash position: 40% left, 60% right — set after the window
            # is mapped so the geometry is finalised.
            self.win3d.after(
                50,
                lambda: paned.sash_place(
                    0, int(paned.winfo_width() * 0.4), 0)
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
            """Switch base map while retaining all previously cached sources."""
            if self.mapview is None:
                return
            url = TILE_SERVERS.get(choice)
            if url:
                try:
                    self.mapview.set_tile_server(url, max_zoom=19)
                except Exception as e:
                    self.append_log(f"** Could not change map source: {e} **")

        def _toggle_nautical_overlay(self):
            """Enable/disable OpenSeaMap without flushing the base-map cache."""
            if self.mapview is None:
                return

            enabled = bool(self.nautical_switch.get())
            overlay = OPENSEAMAP_TILE_SERVER if enabled else None
            try:
                # CachedOverlayMapView redraws against a different in-memory
                # cache namespace and keeps both raw sources in SQLite. No call
                # to set_tile_server() is needed here.
                self.mapview.set_overlay_tile_server(overlay)

                if hasattr(self, "nautical_attribution"):
                    if enabled:
                        self.nautical_attribution.configure(
                            text="© OpenStreetMap contributors   |   Nautical: OpenSeaMap")
                    else:
                        self.nautical_attribution.configure(
                            text="© OpenStreetMap contributors")

                self.append_log(
                    f"** OpenSeaMap nautical overlay "
                    f"{'enabled' if enabled else 'disabled'}. **")
            except Exception as e:
                self.append_log(f"** Could not change nautical overlay: {e} **")

        def _refresh_map(self):
            """Re-draw waypoint markers and connecting lines from the store."""
            if self.wp_map_layer is not None:
                self.wp_map_layer.refresh(structural=True)
                self.append_log("** Map waypoints refreshed. **")

        def _download_visible_tiles(self):
            """Save the visible area to the offline tile database.

            Normal interactive viewing is already cached automatically by
            CachedOverlayMapView. This button remains for explicit pre-loading
            with providers that allow it.
            """
            if self.mapview is None:
                return
            try:
                import tkintermapview
            except ImportError:
                return

            server_name = self.tile_combo.get()
            server_url = TILE_SERVERS.get(server_name)

            # Public OSM tiles require local caching of viewed tiles, but prohibit
            # bulk/offline prefetching. Interactive views are already cached by
            # this file, so do not run OfflineLoader against tile.openstreetmap.org.
            if server_name == "OpenStreetMap":
                self.append_log(
                    "** OpenStreetMap public tiles are cached automatically while "
                    "you view them. Bulk 'Save Offline' downloads are disabled for "
                    "this provider. Select a provider that permits offline "
                    "prefetching for this button. **")
                return

            # Bounding box of the currently visible map area.
            try:
                top_left = self.mapview.convert_canvas_coords_to_decimal_coords(0, 0)
                bottom_right = self.mapview.convert_canvas_coords_to_decimal_coords(
                    self.mapview.winfo_width(), self.mapview.winfo_height())
            except Exception as e:
                self.append_log(f"** Could not read map bounds: {e} **")
                return

            current_zoom = int(self.mapview.zoom)
            zoom_min = max(0, current_zoom - 3)
            zoom_max = min(19, current_zoom + 3)

            # Rough estimate of tile count so the user knows what they're asking for.
            lat_span = abs(top_left[0] - bottom_right[0])
            lon_span = abs(top_left[1] - bottom_right[1])
            est_tiles = 0
            for z in range(zoom_min, zoom_max + 1):
                n = 2 ** z
                est_tiles += max(1, int(n * lon_span / 360.0)) * \
                             max(1, int(n * lat_span / 180.0))

            self.append_log(
                f"** Downloading tiles for zoom {zoom_min}–{zoom_max} "
                f"(~{est_tiles} tiles) from {server_name}... **")
            self.map_download_btn.configure(state="disabled",
                                            text="Downloading...")

            def _worker():
                try:
                    _prepare_tile_cache_database(MAP_CACHE_DB)
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
