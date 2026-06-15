"""Color helpers shared by the raw tkinter canvas widgets.

CTk widgets re-theme themselves on a light/dark switch, but plain tkinter
Canvas widgets don't, so they call these to pick the right color for the
current mode.
"""

import customtkinter as ctk


def resolve_color(color):
    """Pick the light or dark value from a CTk color.

    CTk colors are often a [light, dark] pair. Raw canvases need a single
    value, so return the one matching the current mode.
    """
    if isinstance(color, (list, tuple)):
        return color[0] if ctk.get_appearance_mode() == "Light" else color[1]
    return color


def canvas_bg(master_frame):
    """Return a canvas background that matches its parent CTk frame."""
    try:
        c = resolve_color(master_frame.cget("fg_color"))
    except Exception:
        c = None
    if not c or c == "transparent":
        return "#2b2b2b" if ctk.get_appearance_mode() == "Dark" else "#dbdbdb"
    return c
