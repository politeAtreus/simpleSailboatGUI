"""Prepare a Nova Scotia Lake Inventory map for Sailboat Ground Station.

The NS inventory maps are PDF/image map sheets, not georeferenced XYZ tiles.
This utility performs the one-time local preprocessing step after geographic
bounds have been established for the cropped depth-map image.

Output package:
    lake_depth_maps/<Lake Name>/metadata.json
    lake_depth_maps/<Lake Name>/overlay.png

PNG/JPEG inputs need only Pillow (already used by the project).
PDF inputs additionally require PyMuPDF:
    pip install PyMuPDF

Example:
    python prepare_lake_depth.py banook.pdf \
        --name "Lake Banook" \
        --bounds 44.6900 44.6700 -63.5650 -63.5450 \
        --crop 180 80 1450 1900 \
        --source-url "https://novascotia.ca/fish/documents/lake-inventory-maps/3-H-banook.pdf"

IMPORTANT: --bounds are NORTH SOUTH WEST EAST for the CROPPED image. They
must be calibrated against known shoreline/control points. Do not use rough
lake-centre coordinates as bounds.
"""

import argparse
import json
import os
import re
from pathlib import Path

from PIL import Image, ImageEnhance


def safe_dir_name(name):
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip()
    name = re.sub(r"\s+", "_", name)
    return name or "lake"


def load_source(path, dpi=200):
    path = Path(path)
    if path.suffix.lower() == ".pdf":
        try:
            import fitz  # PyMuPDF
        except ImportError as exc:
            raise SystemExit(
                "PDF input requires PyMuPDF. Install it with: pip install PyMuPDF"
            ) from exc

        doc = fitz.open(str(path))
        if len(doc) < 1:
            raise SystemExit("PDF has no pages")
        page = doc[0]
        scale = float(dpi) / 72.0
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        mode = "RGB" if pix.n < 4 else "RGBA"
        img = Image.frombytes(mode, (pix.width, pix.height), pix.samples)
        doc.close()
        return img.convert("RGBA")

    with Image.open(path) as img:
        img.load()
        return img.convert("RGBA")


def crop_image(img, crop):
    if crop is None:
        return img
    left, top, right, bottom = crop
    left = max(0, min(img.width - 1, left))
    top = max(0, min(img.height - 1, top))
    right = max(left + 1, min(img.width, right))
    bottom = max(top + 1, min(img.height, bottom))
    return img.crop((left, top, right, bottom))


def white_to_alpha(img, threshold=245, ink_boost=1.0):
    """Make paper/background transparent while preserving map ink and colours."""
    img = img.convert("RGBA")
    if ink_boost != 1.0:
        rgb = Image.new("RGB", img.size)
        rgb.paste(img, mask=img.getchannel("A"))
        rgb = ImageEnhance.Contrast(rgb).enhance(float(ink_boost))
        img = rgb.convert("RGBA")

    px = img.load()
    threshold = int(max(1, min(255, threshold)))
    fade_start = max(0, threshold - 24)
    span = max(1, threshold - fade_start)

    for y in range(img.height):
        for x in range(img.width):
            r, g, b, a = px[x, y]
            light = min(r, g, b)
            # White/near-white page becomes transparent. The short fade band
            # reduces ugly halos around anti-aliased contour lines and text.
            if light >= threshold:
                px[x, y] = (r, g, b, 0)
            elif light > fade_start:
                alpha = int(a * (threshold - light) / span)
                px[x, y] = (r, g, b, alpha)
    return img


def main():
    parser = argparse.ArgumentParser(
        description="Prepare a georeferenced NS lake-depth overlay package.")
    parser.add_argument("input", help="Official lake map PDF or image")
    parser.add_argument("--name", required=True, help="Display name, e.g. Lake Banook")
    parser.add_argument(
        "--bounds", required=True, nargs=4, type=float,
        metavar=("NORTH", "SOUTH", "WEST", "EAST"),
        help="Geographic bounds of the CROPPED map image")
    parser.add_argument(
        "--crop", nargs=4, type=int,
        metavar=("LEFT", "TOP", "RIGHT", "BOTTOM"),
        help="Optional pixel crop applied before transparency conversion")
    parser.add_argument("--dpi", type=int, default=200,
                        help="PDF render DPI (default: 200)")
    parser.add_argument("--white-threshold", type=int, default=245,
                        help="Pixels this light become transparent (default: 245)")
    parser.add_argument("--ink-boost", type=float, default=1.0,
                        help="Optional source contrast multiplier (default: 1.0)")
    parser.add_argument("--opacity", type=float, default=0.72,
                        help="Default GUI opacity, 0..1 (default: 0.72)")
    parser.add_argument("--preferred-zoom", type=int, default=15,
                        help="Zoom selected when this lake is enabled")
    parser.add_argument(
        "--depth-contours", nargs="+", type=float, default=[1.0, 3.0, 5.0],
        metavar="M",
        help=("Labelled bathymetry contour depths in metres, shallow to deep "
              "(default: 1 3 5). These drive numeric depth interpolation."))
    parser.add_argument(
        "--depth-error", type=float, default=0.75,
        help=("Nominal +/- uncertainty for interpolated depths in metres "
              "(default: 0.75)"))
    parser.add_argument("--source-url", default="",
                        help="Official source PDF URL stored in metadata")
    parser.add_argument(
        "--output-root", default="lake_depth_maps",
        help="Destination root (default: ./lake_depth_maps)")
    args = parser.parse_args()

    north, south, west, east = args.bounds
    if not north > south:
        raise SystemExit("NORTH must be greater than SOUTH")
    if not east > west:
        raise SystemExit("EAST must be greater than WEST")

    opacity = max(0.05, min(1.0, float(args.opacity)))
    preferred_zoom = max(1, min(19, int(args.preferred_zoom)))
    depth_contours = sorted({float(v) for v in args.depth_contours if v > 0.0})
    if not depth_contours:
        raise SystemExit("--depth-contours must contain at least one positive depth")
    depth_error = max(0.1, float(args.depth_error))

    img = load_source(args.input, dpi=args.dpi)
    img = crop_image(img, args.crop)
    img = white_to_alpha(
        img, threshold=args.white_threshold, ink_boost=args.ink_boost)

    out_dir = Path(args.output_root) / safe_dir_name(args.name)
    out_dir.mkdir(parents=True, exist_ok=True)
    image_path = out_dir / "overlay.png"
    meta_path = out_dir / "metadata.json"

    img.save(image_path, "PNG", optimize=True)
    metadata = {
        "name": args.name,
        "image": "overlay.png",
        "bounds": {
            "north": north,
            "south": south,
            "west": west,
            "east": east,
        },
        "preferred_zoom": preferred_zoom,
        "opacity": opacity,
        "depth_contours_m": depth_contours,
        "depth_uncertainty_m": depth_error,
        "depth_estimation": (
            "Approximate raster-derived interpolation between labelled contours; "
            "not survey-grade navigation data."),
        "source": "Nova Scotia Fisheries & Aquaculture Lake Inventory",
        "source_url": args.source_url,
        "notes": "Bathymetry may not be accurate.",
    }
    meta_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    print(f"Prepared: {args.name}")
    print(f"  image:    {image_path}")
    print(f"  metadata: {meta_path}")
    print("  depth contours (m): " + ", ".join(f"{v:g}" for v in depth_contours))
    print(f"  nominal depth error: +/- {depth_error:g} m")
    print("Use the map window's Lake depth -> Reload button to pick it up.")


if __name__ == "__main__":
    main()
