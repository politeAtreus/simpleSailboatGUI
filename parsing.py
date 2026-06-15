"""Parse serial lines from the controller and format telemetry values.

Two line types come over the COM port:

1. Controller status (printed continuously):
       sail=-19  rudder=0  | dropped=0  overruns=0

2. Telemetry echoed back from the boat over XBee:
       XBee RX: {"tb":0,"tlat":0,...,"wa":0"sa":347,}

   The telemetry payload is almost-JSON but malformed (missing/extra commas),
   so we parse it with a tolerant regex instead of json.loads().
"""

import re

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
