"""Waypoint list shared between the main window panel and the map view.

WaypointStore is the single source of truth. The panel and the map both
subscribe to changes via add_listener() and rebuild their views when
notified.
"""

import itertools
import json
import tkinter as tk

import customtkinter as ctk


# Status of a single waypoint.
STATUS_IDLE    = "idle"     # white  - just in the list
STATUS_NEXT    = "next"     # yellow - next in line
STATUS_ACTIVE  = "active"   # green  - currently active
STATUS_SKIPPED = "skipped"  # red    - skipped by the boat


# Sync state of the whole list (drives the banner).
SYNC_SYNCED      = "synced"
SYNC_UNSYNCED    = "unsynced"
SYNC_EMPTY       = "empty"
SYNC_SKIPPED_HIT = "skipped_hit"


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #

class WaypointStore:
    """Holds the waypoint list and notifies listeners on every change.

    Keeps a snapshot of the last list sent to the boat so it can tell when
    the user has unsent changes.
    """

    def __init__(self):
        self._items = []        # list of dicts: id, name, lat, lon, status
        self._synced_snapshot = []  # last list sent to the boat
        self._listeners = []
        self._id_counter = itertools.count(1)
        self._last_skipped_name = None

    # ----- listeners --------------------------------------------------- #
    def add_listener(self, fn):
        """Register a callback fn() to be called on every change."""
        self._listeners.append(fn)

    def _notify(self, structural=True):
        for fn in self._listeners:
            try:
                fn(structural)
            except TypeError:
                # Listener accepts no args
                try:
                    fn()
                except Exception:
                    pass
            except Exception:
                pass

    # ----- queries ----------------------------------------------------- #
    def items(self):
        """Return a copy of the list (callers shouldn't mutate it directly)."""
        return list(self._items)

    def count(self):
        return len(self._items)

    def sync_state(self):
        """Return (sync_state, detail) for the banner."""
        if self._last_skipped_name is not None:
            return SYNC_SKIPPED_HIT, self._last_skipped_name
        if not self._items:
            return SYNC_EMPTY, None
        if self._content_equals_snapshot():
            # In sync. Surface active waypoint if any.
            for w in self._items:
                if w["status"] == STATUS_ACTIVE:
                    return SYNC_SYNCED, w["name"]
            return SYNC_SYNCED, None
        return SYNC_UNSYNCED, None

    def _content_equals_snapshot(self):
        """Compare current list to last-sent snapshot, ignoring status."""
        if len(self._items) != len(self._synced_snapshot):
            return False
        for a, b in zip(self._items, self._synced_snapshot):
            for k in ("id", "name", "lat", "lon"):
                if a[k] != b[k]:
                    return False
        return True

    # ----- mutations --------------------------------------------------- #
    def add(self, lat, lon, name=None):
        wp = {
            "id":     next(self._id_counter),
            "name":   name or f"WP {len(self._items) + 1}",
            "lat":    float(lat),
            "lon":    float(lon),
            "status": STATUS_IDLE,
        }
        self._items.append(wp)
        self._notify(structural=True)
        return wp

    def remove(self, wp_id):
        self._items = [w for w in self._items if w["id"] != wp_id]
        self._notify(structural=True)

    def rename(self, wp_id, new_name):
        for w in self._items:
            if w["id"] == wp_id:
                if w["name"] == new_name:
                    return
                w["name"] = new_name
                self._notify(structural=True)
                return

    def move(self, from_index, to_index):
        """Move item at from_index to to_index. Clamps to valid range."""
        if from_index == to_index:
            return
        n = len(self._items)
        if not (0 <= from_index < n):
            return
        to_index = max(0, min(n - 1, to_index))
        item = self._items.pop(from_index)
        self._items.insert(to_index, item)
        self._notify(structural=True)

    def clear(self):
        self._items = []
        self._last_skipped_name = None
        self._notify(structural=True)

    def restore_synced(self):
        """Replace current list with the last-sent snapshot."""
        self._items = [dict(w) for w in self._synced_snapshot]
        self._last_skipped_name = None
        self._notify(structural=True)

    def mark_synced(self):
        """Snapshot the current list as the version sent to the boat."""
        self._synced_snapshot = [dict(w) for w in self._items]
        self._last_skipped_name = None
        self._notify(structural=False)

    def set_status(self, wp_id, status):
        for w in self._items:
            if w["id"] == wp_id:
                w["status"] = status
                if status == STATUS_SKIPPED:
                    self._last_skipped_name = w["name"]
                self._notify(structural=False)
                return

    def clear_skipped_alert(self):
        self._last_skipped_name = None
        self._notify(structural=False)

    # ----- export ------------------------------------------------------ #
    def to_json(self):
        """Build the JSON payload to send to the boat."""
        payload = {
            "waypoints": [
                {"id": w["id"], "name": w["name"],
                 "lat": w["lat"], "lon": w["lon"]}
                for w in self._items
            ]
        }
        return json.dumps(payload)


# --------------------------------------------------------------------------- #
# Panel
# --------------------------------------------------------------------------- #

# Status colors for the indicator dot.
_DOT_COLORS = {
    STATUS_IDLE:    "#ffffff",
    STATUS_NEXT:    "#e8c43d",
    STATUS_ACTIVE:  "#4caf72",
    STATUS_SKIPPED: "#d05b5b",
}


class WaypointPanel(ctk.CTkFrame):
    """Side panel with status banner, scrollable list, and action buttons."""

    def __init__(self, master, store, on_send, on_log, **kwargs):
        super().__init__(master, corner_radius=10, **kwargs)
        self.store    = store
        self.on_send  = on_send   # callback when "Send to Boat" is pressed
        self.on_log   = on_log    # callback for log messages

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        self._build_header()
        self._build_banner()
        self._build_list()
        self._build_buttons()

        # Drag state
        self._drag_from = None
        self._drag_widget = None

        store.add_listener(self.refresh)
        self.refresh()

    # ----- layout ------------------------------------------------------ #
    def _build_header(self):
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 4))
        head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(head, text="Waypoints",
                     font=ctk.CTkFont(size=15, weight="bold")).grid(
            row=0, column=0, sticky="w")
        self.count_label = ctk.CTkLabel(head, text="0",
                                        text_color=("gray45", "gray60"),
                                        font=ctk.CTkFont(size=11))
        self.count_label.grid(row=0, column=1, sticky="e")

    def _build_banner(self):
        self.banner = ctk.CTkLabel(
            self, text="No Waypoints", anchor="w",
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color=("#e0e4ec", "#2a2e3a"), corner_radius=6,
            text_color=("#283040", "#c8ccdc"))
        self.banner.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 8),
                         ipady=6)

    def _build_list(self):
        self.list_frame = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.list_frame.grid(row=2, column=0, sticky="nsew", padx=8, pady=0)
        self.list_frame.grid_columnconfigure(0, weight=1)
        self._row_widgets = []   # parallel to store.items()

    def _build_buttons(self):
        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.grid(row=3, column=0, sticky="ew", padx=12, pady=10)
        btns.grid_columnconfigure((0, 1, 2), weight=1)
        self.send_btn = ctk.CTkButton(
            btns, text="Send to Boat", command=self._on_send,
            fg_color="#3b8ed0", hover_color="#2f7ab5")
        self.send_btn.grid(row=0, column=0, padx=2, sticky="ew")
        ctk.CTkButton(
            btns, text="Restore", command=self._on_restore,
            fg_color="transparent", border_width=1,
            text_color=("gray20", "gray80")).grid(
            row=0, column=1, padx=2, sticky="ew")
        ctk.CTkButton(
            btns, text="Clear All", command=self._on_clear,
            fg_color="#c0552e", hover_color="#a8482a").grid(
            row=0, column=2, padx=2, sticky="ew")

    # ----- refresh ----------------------------------------------------- #
    def refresh(self, structural=True):
        """Rebuild from the store. structural=True rebuilds the list rows;
        structural=False only updates dots and banner so in-progress edits
        aren't disturbed.
        """
        self._refresh_banner()
        if structural:
            self._refresh_list()
        else:
            self._refresh_status_only()

    def _refresh_banner(self):
        state, detail = self.store.sync_state()
        n = self.store.count()
        self.count_label.configure(text=f"{n}" if n else "0")

        if state == SYNC_SKIPPED_HIT:
            text  = f"Waypoint {detail} Skipped!"
            color = ("#d05b5b", "#d05b5b")
        elif state == SYNC_UNSYNCED:
            text  = "ALERT: New Changes not Transmitted"
            color = ("#d0a23b", "#d0a23b")
        elif state == SYNC_EMPTY:
            text  = "No Waypoints"
            color = ("#283040", "#c8ccdc")
        elif state == SYNC_SYNCED and detail:
            text  = f"Active Waypoint is {detail}"
            color = ("#4caf72", "#4caf72")
        else:
            text  = "Synced to Boat"
            color = ("#4caf72", "#4caf72")

        self.banner.configure(text=text, text_color=color)

    def _refresh_list(self):
        # Tear down old rows
        for w in self._row_widgets:
            w.destroy()
        self._row_widgets = []

        for i, wp in enumerate(self.store.items()):
            row = self._make_row(i, wp)
            row.grid(row=i, column=0, sticky="ew", padx=4, pady=2)
            self._row_widgets.append(row)

    def _refresh_status_only(self):
        """Update dot colors in place without rebuilding rows."""
        items = self.store.items()
        # If lengths mismatch (e.g. listener fired out of order), fall back.
        if len(items) != len(self._row_widgets):
            self._refresh_list()
            return
        for wp, row in zip(items, self._row_widgets):
            try:
                row._dot.itemconfigure(
                    "dot", fill=_DOT_COLORS.get(wp["status"], "#ffffff"))
            except Exception:
                pass

    # ----- row construction ------------------------------------------- #
    def _make_row(self, index, wp):
        row = ctk.CTkFrame(self.list_frame, corner_radius=6,
                           fg_color=("#e6e6ec", "#26262e"))
        row.grid_columnconfigure(2, weight=1)
        row._wp_id = wp["id"]
        row._index = index

        # Indicator dot (canvas circle)
        dot = tk.Canvas(row, width=14, height=14, highlightthickness=0,
                        bd=0, bg=self._row_bg())
        dot.create_oval(2, 2, 12, 12,
                        fill=_DOT_COLORS.get(wp["status"], "#ffffff"),
                        outline="#555", tags="dot")
        dot.grid(row=0, column=0, padx=(8, 4), pady=8)
        row._dot = dot

        # Drag handle
        handle = ctk.CTkLabel(row, text="\u2630", width=18,
                              font=ctk.CTkFont(size=14),
                              text_color=("gray45", "gray60"))
        handle.grid(row=0, column=1, padx=(0, 6))
        handle.configure(cursor="fleur")
        handle.bind("<ButtonPress-1>",   lambda e, r=row: self._drag_start(e, r))
        handle.bind("<B1-Motion>",       lambda e, r=row: self._drag_motion(e, r))
        handle.bind("<ButtonRelease-1>", lambda e, r=row: self._drag_end(e, r))

        # Editable name + coords
        info = ctk.CTkFrame(row, fg_color="transparent")
        info.grid(row=0, column=2, sticky="ew", padx=2, pady=4)
        info.grid_columnconfigure(0, weight=1)

        name_var = tk.StringVar(value=wp["name"])
        name_entry = ctk.CTkEntry(info, textvariable=name_var,
                                  font=ctk.CTkFont(size=12, weight="bold"),
                                  height=24, border_width=0,
                                  fg_color="transparent")
        name_entry.grid(row=0, column=0, sticky="ew")
        name_entry.bind("<FocusOut>",
                        lambda e, wid=wp["id"], v=name_var:
                            self.store.rename(wid, v.get().strip() or "WP"))
        name_entry.bind("<Return>", lambda e: self.focus())

        coords = ctk.CTkLabel(
            info, text=f"{wp['lat']:.5f}, {wp['lon']:.5f}",
            font=ctk.CTkFont(size=10),
            text_color=("gray45", "gray60"), anchor="w")
        coords.grid(row=1, column=0, sticky="w")

        # Delete button
        ctk.CTkButton(row, text="\u2715", width=24, height=24,
                      fg_color="transparent",
                      hover_color=("gray75", "gray30"),
                      text_color=("gray45", "gray60"),
                      command=lambda wid=wp["id"]: self.store.remove(wid)).grid(
            row=0, column=3, padx=(0, 6))
        return row

    def _row_bg(self):
        return "#26262e" if ctk.get_appearance_mode() == "Dark" else "#e6e6ec"

    # ----- drag-to-reorder -------------------------------------------- #
    def _drag_start(self, event, row):
        self._drag_from = row._index

    def _drag_motion(self, event, row):
        if self._drag_from is None:
            return
        # Convert event y to a y in the scrollable frame's coordinate space.
        y_root = event.y_root
        # Find which row currently sits under the cursor.
        for r in self._row_widgets:
            top = r.winfo_rooty()
            bot = top + r.winfo_height()
            if top <= y_root <= bot:
                target = r._index
                if target != self._drag_from:
                    self.store.move(self._drag_from, target)
                    self._drag_from = target
                return

    def _drag_end(self, event, row):
        self._drag_from = None

    # ----- button handlers -------------------------------------------- #
    def _on_send(self):
        if self.store.count() == 0:
            self.on_log("** No waypoints to send. **")
            return
        payload = self.store.to_json()
        self.on_send(payload)
        self.store.mark_synced()
        self.on_log(f"** Sent {self.store.count()} waypoints to boat. **")

    def _on_restore(self):
        self.store.restore_synced()
        self.on_log("** Restored last synced waypoint list. **")

    def _on_clear(self):
        self.store.clear()
        self.on_log("** Cleared all waypoints. **")


# --------------------------------------------------------------------------- #
# Map integration
# --------------------------------------------------------------------------- #

class WaypointMapLayer:
    """Draws waypoints and connecting lines on a tkintermapview map.

    Subscribes to the store and rebuilds its markers/paths on every change.
    The boat marker and breadcrumb path are managed elsewhere and aren't
    touched by this class.
    """

    def __init__(self, mapview, store):
        self.mapview = mapview
        self.store   = store
        self._markers = []
        self._path = None
        store.add_listener(self.refresh)
        self.refresh()

    def refresh(self, structural=True):
        # Tear down old markers and path
        for m in self._markers:
            try:
                m.delete()
            except Exception:
                pass
        self._markers = []
        if self._path is not None:
            try:
                self._path.delete()
            except Exception:
                pass
            self._path = None

        items = self.store.items()
        if not items:
            return

        # Place numbered markers
        for i, wp in enumerate(items, start=1):
            try:
                marker = self.mapview.set_marker(
                    wp["lat"], wp["lon"], text=f"{i}. {wp['name']}",
                    marker_color_circle=_DOT_COLORS.get(wp["status"], "#ffffff"),
                    marker_color_outside="#444")
                self._markers.append(marker)
            except Exception:
                pass

        # Connect them with a path in list order
        if len(items) >= 2:
            try:
                coords = [(w["lat"], w["lon"]) for w in items]
                self._path = self.mapview.set_path(coords)
            except Exception:
                self._path = None
