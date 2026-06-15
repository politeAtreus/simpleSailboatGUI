"""Serial port detection and the background reader thread."""

import threading

import serial
import serial.tools.list_ports


def is_stlink(port) -> bool:
    """True if a pyserial port looks like an ST-Link Virtual COM Port.

    Matches the description text (Windows uses several wordings) and falls
    back to the STMicroelectronics USB vendor ID 0x0483.
    """
    desc = (port.description or "").lower()
    if "stlink" in desc or "st-link" in desc or "st link" in desc:
        return True
    if getattr(port, "vid", None) == 0x0483:
        return True
    return False


class SerialReader(threading.Thread):
    """Read full lines off the serial port and push them to a queue.

    Runs in a background thread so the GUI never blocks. Widget updates
    happen on the main thread via the queue, because tkinter isn't
    thread-safe.
    """

    def __init__(self, ser, out_queue, stop_event):
        super().__init__(daemon=True)
        self.ser = ser
        self.out_queue = out_queue
        self.stop_event = stop_event

    def run(self):
        while not self.stop_event.is_set():
            try:
                raw = self.ser.readline()  # blocks up to the port timeout
            except (serial.SerialException, OSError) as e:
                self.out_queue.put(("error", f"Serial error: {e}"))
                return
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace").strip("\r\n")
            if line:
                self.out_queue.put(("line", line))
