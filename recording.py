"""Write telemetry to CSV and export the GPS track as GPX.

Recorder owns the file handles and row count. It does no UI work: callers
pass data in and use the returned status strings to update widgets.
"""

import csv
import os
from datetime import datetime

from parsing import TELEMETRY_FIELDS


class Recorder:
    def __init__(self, log_dir="logs"):
        self.log_dir = log_dir
        self.file = None
        self.writer = None
        self.rows = 0
        self.path = None

    @property
    def active(self):
        return self.file is not None

    def start(self):
        """Open a new CSV. Return (ok, message)."""
        try:
            os.makedirs(self.log_dir, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.path = os.path.join(self.log_dir, f"telemetry_{stamp}.csv")
            self.file = open(self.path, "w", newline="")
        except OSError as e:
            self.file = None
            return False, f"Could not start recording: {e}"
        self.writer = csv.writer(self.file)
        self.writer.writerow(["timestamp"] + [k for k, _, _ in TELEMETRY_FIELDS])
        self.rows = 0
        return True, f"Recording to {self.path}"

    def stop(self):
        """Close the CSV. Return a status message."""
        if self.file is not None:
            try:
                self.file.close()
            except Exception:
                pass
        msg = f"Recording stopped: {self.path} ({self.rows} rows)"
        self.file = None
        self.writer = None
        return msg

    def write_row(self, d):
        """Append one telemetry sample. No-op if not recording."""
        if self.writer is None:
            return
        row = [datetime.now().isoformat(timespec="milliseconds")]
        row += [d.get(k, "") for k, _, _ in TELEMETRY_FIELDS]
        try:
            self.writer.writerow(row)
            self.rows += 1
        except Exception:
            pass

    def status_text(self):
        """Short label for the recording state."""
        if self.active:
            return f"\u25cf REC  {self.rows} rows"
        return "not recording"

    def export_gpx(self, gps_track):
        """Write the GPS track to a GPX file. Return (ok, message)."""
        if not gps_track:
            return False, "No GPS points to export yet."
        try:
            os.makedirs(self.log_dir, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = os.path.join(self.log_dir, f"track_{stamp}.gpx")
            with open(path, "w", encoding="utf-8") as f:
                f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
                f.write('<gpx version="1.1" creator="Sailboat Ground Station '
                        'Monitor" xmlns="http://www.topografix.com/GPX/1/1">\n')
                f.write(' <trk><name>Sailboat track</name><trkseg>\n')
                for lat, lon, t in gps_track:
                    f.write(f'  <trkpt lat="{lat:.7f}" lon="{lon:.7f}">'
                            f'<time>{t.isoformat()}</time></trkpt>\n')
                f.write(' </trkseg></trk>\n</gpx>\n')
            return True, f"Exported {len(gps_track)} points to {path}"
        except OSError as e:
            return False, f"GPX export failed: {e}"
