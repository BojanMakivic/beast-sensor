# Beast Sensor Bar-Velocity Tracker

A Windows command-line tracker for a Beast Bluetooth Low Energy sensor. It connects directly to the configured sensor, detects bottom-to-top repetitions, estimates ascent time, displacement, average speed, and peak speed, and writes the results to an Excel workbook.

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

Keep the sensor still during the three-second calibration. Stop the tracker with `Ctrl+C`.

Each completed ascent prints:

```text
REP 01 | time 1.20 s | displacement 0.650 m | average speed 0.542 m/s | peak speed 0.910 m/s
```

The generated files are stored locally:

- `beast_repetitions.json` contains the source measurements.
- `outputs/beast_tracker/Beast Workout.xlsx` contains the formatted Excel log.

These runtime files are ignored by Git so every machine starts with a clean training log.

## How repetition detection works

The sensor quaternion is used to rotate acceleration into the vertical world axis. The signal is filtered to reduce bias and drift, then integrated into vertical velocity.

- A repetition starts when sustained upward acceleration is detected.
- The ascent ends when vertical velocity reaches zero and reverses at the top.
- Downward travel is ignored.
- The detector re-arms when downward velocity reaches zero at the bottom.

Accelerometer-only displacement is an estimate and should be checked against a known bar travel distance.

## Optional sensor diagnostics

`probe_imu_characteristics.py` can be used when investigating the sensor characteristics. It requires the same `.venv`.

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
