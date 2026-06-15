"""3D boat view built with matplotlib.

Uses Poly3DCollection with matplotlib's depth buffer so the boat renders
correctly at any heading. Mouse rotate and zoom come free from matplotlib.
"""

import math
import tkinter as tk

import customtkinter as ctk
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from mpl_toolkits.mplot3d.art3d import Poly3DCollection


class Boat3DView(tk.Frame):
    """3D boat view using matplotlib Poly3DCollection.

    Replaces the hand-rolled painter's algorithm with matplotlib's proper
    depth buffer so the boat renders correctly at any heading. Mouse
    interaction (rotate, zoom) comes free from matplotlib.
    """

    SAIL_COLOR   = "#3b8ed0"
    RUDDER_COLOR = "#e8a33d"
    _INIT_ELEV   = 25.0
    _INIT_AZIM   = 225.0

    def __init__(self, master, width=460, height=460, **kwargs):
        for k in ("highlightthickness",):
            kwargs.pop(k, None)
        super().__init__(master, **kwargs)
        self.master_frame  = master
        self.heading       = 0.0
        self.sail_angle    = 0.0
        self.rudder_angle  = 0.0

        self.fig = Figure(figsize=(width / 100.0, height / 100.0), dpi=100)
        self.fig.subplots_adjust(left=0, right=1, top=1, bottom=0)
        self.ax  = self.fig.add_subplot(111, projection="3d")

        self.mpl_canvas = FigureCanvasTkAgg(self.fig, master=self)
        self.mpl_canvas.get_tk_widget().grid(row=0, column=0, sticky="nsew")
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self._draw()

    # ------------------------------------------------------------------ #
    def set_state(self, heading, sail, rudder):
        self.heading      = float(heading)
        self.sail_angle   = float(sail)
        self.rudder_angle = float(rudder)
        self._draw()

    def refresh_theme(self):
        self._draw()

    # ------------------------------------------------------------------ #
    def _palette(self):
        dark = ctk.get_appearance_mode() == "Dark"
        if not dark:
            return {"water": "#b9c4cf", "deck": "#dde4ec",
                    "hull_stbd": "#8090a0", "hull_port": "#707888",
                    "hull_dark": "#505868", "keel": "#353c4a",
                    "mast": "#404858", "label": "#283040",
                    "outline": "#1c2430", "bg": "#f0f2f8"}
        return {"water": "#2f3a44",   "deck": "#7a8898",
                "hull_stbd": "#32394c", "hull_port": "#22283a",
                "hull_dark": "#13151e",  "keel": "#0c0e14",
                "mast": "#d0d4e0",      "label": "#c8ccdc",
                "outline": "#080a10",   "bg": "#1c1c24"}

    @staticmethod
    def _rot_z(pts, deg):
        """Rotate (x,y,z) list about the z-axis."""
        a = math.radians(deg)
        c, s = math.cos(a), math.sin(a)
        return [(x * c - y * s, x * s + y * c, z) for x, y, z in pts]

    @staticmethod
    def _rot_z_about(pts, deg, ox, oy):
        """Rotate (x,y,z) list about a vertical axis through (ox, oy)."""
        a = math.radians(deg)
        c, s = math.cos(a), math.sin(a)
        out = []
        for x, y, z in pts:
            dx, dy = x - ox, y - oy
            out.append((ox + dx * c - dy * s, oy + dx * s + dy * c, z))
        return out

    def _draw(self):
        pal = self._palette()

        # Preserve the user's rotated viewpoint across redraws.
        try:
            elev, azim = self.ax.elev, self.ax.azim
        except Exception:
            elev, azim = self._INIT_ELEV, self._INIT_AZIM

        self.ax.cla()

        # Background + clean axes
        self.fig.patch.set_facecolor(pal["bg"])
        self.ax.set_facecolor(pal["bg"])
        self.ax.set_axis_off()
        self.ax.view_init(elev=elev, azim=azim)
        for pane in (self.ax.xaxis.pane, self.ax.yaxis.pane,
                     self.ax.zaxis.pane):
            pane.fill = False
            pane.set_edgecolor("none")

        # ---- boat geometry (boat frame: y=bow, x=stbd, z=up) ----
        deck_z, bot_z = 0.16, -0.22
        top = [(0, 1.85, deck_z), (0.20, 1.05, deck_z), (0.30, 0.0, deck_z),
               (0.26, -1.05, deck_z), (0.15, -1.6, deck_z),
               (-0.15, -1.6, deck_z), (-0.26, -1.05, deck_z),
               (-0.30, 0.0, deck_z), (-0.20, 1.05, deck_z)]
        bot = [(0, 1.7, bot_z), (0.10, 1.05, bot_z), (0.15, 0.0, bot_z),
               (0.13, -1.05, bot_z), (0.07, -1.5, bot_z),
               (-0.07, -1.5, bot_z), (-0.13, -1.05, bot_z),
               (-0.15, 0.0, bot_z), (-0.10, 1.05, bot_z)]

        def H(pts):
            return self._rot_z(pts, -self.heading)

        top_h = H(top)
        bot_h = H(bot)

        # Deck and hull bottom
        self.ax.add_collection3d(Poly3DCollection(
            [top_h], facecolors=pal["deck"],
            edgecolors=pal["outline"], linewidths=0.8))
        self.ax.add_collection3d(Poly3DCollection(
            [bot_h], facecolors=pal["hull_dark"],
            edgecolors=pal["outline"], linewidths=0.8))

        # Hull sides — port panels darker than starboard for depth cue
        stbd, port = [], []
        m = len(top)
        for i in range(m):
            j = (i + 1) % m
            face = [top_h[i], top_h[j], bot_h[j], bot_h[i]]
            if (top[i][0] + top[j][0]) >= 0:
                stbd.append(face)
            else:
                port.append(face)
        self.ax.add_collection3d(Poly3DCollection(
            stbd, facecolors=pal["hull_stbd"],
            edgecolors=pal["outline"], linewidths=0.8))
        self.ax.add_collection3d(Poly3DCollection(
            port, facecolors=pal["hull_port"],
            edgecolors=pal["outline"], linewidths=0.8))

        # Keel
        keel_y, keel_d = 0.05, -1.85
        keel_pts = H([(0, keel_y + 0.32, bot_z), (0, keel_y - 0.34, bot_z),
                      (0, keel_y - 0.10, keel_d), (0, keel_y + 0.10, keel_d)])
        self.ax.add_collection3d(Poly3DCollection(
            [keel_pts], facecolors=pal["keel"],
            edgecolors=pal["keel"], linewidths=0.5))

        # Rudder (yaws with rudder angle)
        rpx, rpy = 0.0, -1.50
        rud_raw = [(0, -1.44, -0.16), (0, -1.60, -0.16),
                   (0, -1.54, -0.92), (0, -1.38, -0.92)]
        rud_pts = H(self._rot_z_about(rud_raw, self.rudder_angle, rpx, rpy))
        self.ax.add_collection3d(Poly3DCollection(
            [rud_pts], facecolors=self.RUDDER_COLOR,
            edgecolors=self.RUDDER_COLOR, linewidths=0.5))

        # Wing sail
        th = math.radians(self.sail_angle)
        dx_s, dy_s = math.sin(th), math.cos(th)
        chord, wing_top_z, mast_y = 0.62, 2.7, 0.0
        le_b = (0, mast_y, deck_z);            le_t = (0, mast_y, wing_top_z)
        te_b = (chord * dx_s, mast_y + chord * dy_s, deck_z)
        te_t = (chord * dx_s, mast_y + chord * dy_s, wing_top_z)
        sail_pts = H([le_b, te_b, te_t, le_t])
        self.ax.add_collection3d(Poly3DCollection(
            [sail_pts], facecolors=self.SAIL_COLOR,
            edgecolors="#1c2e3c", linewidths=1.0, alpha=0.9))

        # Mast pole and bow wind-sensor
        le_b_h, le_t_h = H([le_b])[0], H([le_t])[0]
        self.ax.plot([le_b_h[0], le_t_h[0]], [le_b_h[1], le_t_h[1]],
                    [le_b_h[2], le_t_h[2]], color=pal["mast"], linewidth=2)
        wp = H([(0, 0.95, deck_z), (0, 0.95, deck_z + 0.55)])
        self.ax.plot([wp[0][0], wp[1][0]], [wp[0][1], wp[1][1]],
                    [wp[0][2], wp[1][2]], color=pal["mast"], linewidth=1.5)
        self.ax.scatter(*wp[1], color=pal["deck"], s=20, zorder=5)

        # Water grid
        for v in (-2.4, -1.2, 0, 1.2, 2.4):
            self.ax.plot([-2.4, 2.4], [v, v], [0, 0],
                        color=pal["water"], lw=0.5)
            self.ax.plot([v, v], [-2.4, 2.4], [0, 0],
                        color=pal["water"], lw=0.5)

        self.ax.set_xlim(-2.5, 2.5)
        self.ax.set_ylim(-2.5, 2.5)
        self.ax.set_zlim(-2.0, 3.2)
        self.ax.set_box_aspect([1, 1, 1.3])

        # Captions in axes-space so they're never occluded by 3D geometry
        kw = dict(transform=self.ax.transAxes, fontsize=14,
                  fontweight="bold", va="top")
        self.ax.text2D(0.03, 0.97, f"Heading {self.heading:.0f}\u00b0",
                      color=pal["label"], **kw)
        self.ax.text2D(0.03, 0.88, f"Sail {self.sail_angle:.0f}\u00b0",
                      color=self.SAIL_COLOR, **kw)
        self.ax.text2D(0.03, 0.79, f"Rudder {self.rudder_angle:.0f}\u00b0",
                      color=self.RUDDER_COLOR, **kw)

        self.mpl_canvas.draw_idle()
