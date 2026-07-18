<img width="1672" height="941" alt="beast landscape" src="https://github.com/user-attachments/assets/1b2a8c7c-f398-4560-8b48-43c5cca6950c" />



# Beast Sensor Bar-Velocity Tracker

A Windows command-line tracker for a Beast Bluetooth Low Energy sensor. It
counts every upward phase between the bottom and top direction reversals,
calculates concentric time, displacement, average velocity, and peak velocity,
and writes accepted repetitions to an Excel workbook.

## Requirements

- Windows 10 or Windows 11 with Bluetooth Low Energy
- Python 3.13, matching `.python-version`
- Beast sensor address `BE:A5:7F:30:78:68`

The device address and IMU characteristic UUID are configured near the top of `beast sensor.py`.

## Install

Open PowerShell in the cloned repository:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\setup.ps1
```

The setup script creates `.venv` and installs the exact versions from `requirements.txt`.
It looks for the required Python version with the Windows Python Launcher (`py`),
versioned Python commands such as `python3.13`, normal `python` entries on PATH,
and common Windows install locations such as `C:\Python313\python.exe`.

If setup cannot find Python 3.13, install it from python.org or add an existing
Python 3.13 installation to PATH, then rerun `.\setup.ps1`. Internet access is
required the first time dependencies are downloaded.

## Run

```powershell
.\run.ps1
```

No exercise profile or expected distance is required. Keep the sensor still
until calibration reports `Ready`, then start moving. Stop the tracker with
`Ctrl+C`.

To keep a raw recording and print direction transitions:

```powershell
.\run.ps1 --record --diagnostic
```

Each completed ascent prints:

```text
REP 01 | time 1.20 s | displacement 0.650 m | average speed 0.542 m/s | peak speed 0.910 m/s
```

The generated files are stored locally:

- `beast_repetitions.json` contains accepted repetition measurements.
- `outputs/beast_tracker/Beast Workout.xlsx` contains the formatted Excel log.
- `outputs/recordings/*.jsonl` contains optional raw sensor recordings.

These runtime files are ignored by Git so every machine starts with a clean training log.

## How repetition detection works

The packet is decoded as a 16-bit sequence number, an `x,y,z,w` quaternion, and
XYZ acceleration. Acceleration is rotated onto world Z and gravity is removed
using the stationary calibration.

The packet sequence supplies a 50 Hz sensor clock. Host Bluetooth callback times
are deliberately not used for integration because Windows may deliver packets
in bursts.

The detector follows four states:

```text
REST/BOTTOM -> UP -> TOP/REP -> DOWN -> BOTTOM
```

- Sustained positive vertical acceleration starts an upward phase.
- Acceleration becoming negative means the bar is decelerating but may still be
  moving upward.
- The top is the point where velocity crosses from positive to negative. The
  repetition is counted at that crossing.
- Downward movement is ignored for repetition metrics.
- The next bottom is where velocity crosses from negative to positive, or where
  the bar becomes stationary after moving downward.

Distance never decides whether a repetition is valid. It is calculated after
the bottom and top boundaries are detected.

## Replay a recording

Replay applies the current detector to the exact saved packets without changing
the workout history:

```powershell
.\run.ps1 replay .\outputs\recordings\bench-20260718-131804.jsonl --diagnostic
```

Use the actual filename shown when recording starts. Angle brackets such as
`<recording>` are documentation placeholders and must not be typed literally.

## Optional sensor diagnostics

`probe_imu_characteristics.py` can be used when investigating the sensor characteristics. It requires the same `.venv`.

Run the automated tests with:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## Upload to GitHub

After preparing an empty GitHub repository:

```powershell
git init
git add .
git commit -m "Initial portable Beast sensor tracker"
git branch -M main
git remote add origin https://github.com/YOUR-USERNAME/beast-sensor.git
git push -u origin main
```
