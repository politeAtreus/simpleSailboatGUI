# Sailboat Ground Station Monitor

A desktop app for monitoring the serial output of the STM32 joystick controller used
in the autonomous sailboat project. It shows live sail/rudder gauges, two top-down boat
schematics (your **commanded** angles vs. the boat's **actual** reported angles), the
full XBee telemetry panel, and a raw serial log — with automatic COM-port detection and
ST-Link auto-connect.

> **Platform:** Windows. The app talks to the controller over its ST-Link Virtual COM
> port.

---

## What you need

1. **Python** (installed correctly — see below).
2. Two Python packages: **`customtkinter`** and **`pyserial`** (listed in `requirements.txt`, installed in one step below).
3. This project's files (`sailboat_monitor.py`).

If you already have Python on your PATH, skip to the [Quick start](#quick-start-if-you-already-have-python).

---

## Step 1 — Install Python (do this carefully)

This is the step people get wrong. Follow it exactly.

1. Open this page in your browser:
   **<https://www.python.org/downloads/windows/>**

   > Don't use the Microsoft Store version, and don't just type `python` into the
   > terminal (that opens the Store). Use the official installer from the page above.

2. Under **"Stable Releases"**, find the newest version at the top, then click
   **"Windows installer (64-bit)"**. This downloads a file named something like
   `python-3.x.x-amd64.exe`.

3. **Double-click the downloaded `.exe`** to start the installer.

4. **⚠️ MOST IMPORTANT STEP:** On the very first screen, at the **bottom**, tick the
   checkbox:

   > ☑ **Add python.exe to PATH**
   > *(older installers call this "Add Python 3.x to PATH")*

   If you miss this, Windows won't know where Python is and nothing below will work.

5. Now click the big **"Install Now"** button and wait for it to finish.

6. (Optional but recommended) If, at the end, you see a button called
   **"Disable path length limit"**, click it, then close the installer.

### Check that it worked

1. Press the **Windows key**, type `cmd`, and open **Command Prompt**.
   *(Open a **new** window — one that was already open won't know about the new PATH.)*
2. Type this and press Enter:

   ```
   python --version
   ```

   You should see something like `Python 3.x.x`. 

3. Type this and press Enter:

   ```
   pip --version
   ```

   You should see a `pip 2x.x ...` line. 

If both commands print a version, Python is installed correctly. If you get
*"'python' is not recognized..."*, PATH wasn't ticked — see [Troubleshooting](#troubleshooting).

---

## Step 2 — Get the project files

**Option A (easiest):**
1. On this repo's GitHub page, click the green **`< > Code`** button.
2. Choose **"Download ZIP"**.
3. Right-click the downloaded ZIP → **Extract All...** → pick a folder you'll remember
   (e.g. `C:\Sailboat`).

**Option B (if you use Git):**
```
git clone <this-repo-url>
```

Either way, you should end up with a folder containing all the project files
(see below). Keep them in the same folder — they import each other.

### Project files

The code is split into modules by feature:

| File | What it does |
|------|--------------|
| `sailboat_monitor.py` | Main app and window. Run this one. |
| `parsing.py` | Reads the controller and telemetry serial lines, formats values. |
| `serial_io.py` | Detects ST-Link ports, reads the port in a background thread. |
| `widgets.py` | 2D gauges, top-down boat view, wind rose, trend plot. |
| `boat3d.py` | 3D boat view (matplotlib). |
| `recording.py` | Saves telemetry to CSV and exports the GPS track as GPX. |
| `theme.py` | Light/dark color helpers shared by the widgets. |

---

## Step 3 — Install the required packages

1. Open **Command Prompt** again.
2. Go to the folder where you put the files (the one that contains `requirements.txt`).
   For example:

   ```
   cd C:\Sailboat
   ```

3. Install everything the app needs with one command:

   ```
   pip install -r requirements.txt
   ```

   *(If `pip` isn't recognized, use `python -m pip install -r requirements.txt` instead.)*

Wait for it to say it installed successfully. You only have to do this once.

---

## Step 4 — Run the app

You should still be in the project folder from Step 3. (If you closed the window, open
Command Prompt and `cd` back into it, e.g. `cd C:\Sailboat`.) Then start the app:

```
python sailboat_monitor.py
```

The window should open. (You *can* also just double-click `sailboat_monitor.py`, but
running it from Command Prompt the first time is better — if anything goes wrong, you'll
see the error message instead of a window that silently never appears.)

---

## Using it

1. **Plug in the controller** (the STM32 board, via its ST-Link USB). With
   **"Auto-connect..."** ticked, the app grabs the ST-Link COM port automatically. Or
   pick it from the **COM Port** dropdown and click **Connect**.
2. Make sure **Baud** matches your debug UART (default is **115200**). If the log shows
   garbled characters, the baud is wrong.
3. You'll see:
   - **Sail / Rudder gauges** — your joystick commands.
   - **Commanded** boat tile — where the sail *should* be (modeled).
   - **Actual** boat tile — where the boat *reports* the sail is (from telemetry).
   - **Sailboat Telemetry** panel — the full XBee data.
   - **Raw Serial Log** — everything coming over the port.
4. Use the **Dark Mode** switch top-right to toggle light/dark.

---

## Troubleshooting

| Problem | Fix |
| --- | --- |
| `'python' is not recognized...` | The "Add python.exe to PATH" box wasn't ticked. Re-run the installer, choose **Modify**, and make sure PATH is added — or reinstall and tick the box. Then open a **new** Command Prompt. |
| `'pip' is not recognized...` | Use `python -m pip install customtkinter pyserial` instead. |
| `ModuleNotFoundError: No module named 'customtkinter'` (or `serial`) | You skipped Step 3. From the project folder, run `pip install -r requirements.txt`. |
| Window never opens / closes instantly | Run it from Command Prompt (Step 4) to see the actual error. |
| No COM ports in the dropdown | Check the USB cable, that the ST-Link drivers are installed, and click **Refresh**. |
| Log shows garbled text | Wrong **Baud** rate — set it to match your controller's debug UART. |
| Typing `python` opens the Microsoft Store | You installed the Store stub instead of the real Python. Install from <https://www.python.org/downloads/windows/> as in Step 1. |

---

## Quick start (if you already have Python)

From the project folder:

```
pip install -r requirements.txt
python sailboat_monitor.py
```

---

## Notes for tuning

A couple of constants near the top of `sailboat_monitor.py` you may want to adjust:

- **`SAIL_ROTATION_RATE_DPS`** — slew rate (deg/s) the *commanded* sail is animated at.
- **`SAIL_CMD_DEADBAND`** — joystick deadband below which the commanded sail holds still.
