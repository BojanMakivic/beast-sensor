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

No exercise profile or expected distance is required. The sensor may be
mounted at any angle, but it must be rigidly attached to the bar. Keep it still
until calibration reports `Ready`, then start moving. The live quaternion
compensates for normal bar rotation during a repetition; sensor slippage or
strong rotational acceleration can reduce accuracy. Stop the tracker with
`Ctrl+C`. Plain live runs automatically save raw packets in
`outputs/recordings/` and keep detailed state transitions hidden.

For a bench-press session with only normal status and accepted-repetition
output:

```powershell
.\run.ps1 --exercise bench
```

Add `--diagnostic` only when you want to see bottom, top, rest, rejected
candidate, and packet-gap messages. Use `--no-record` if a live session should
not save raw packets.

Available profiles are `generic`, `bench`, `squat`, and `deadlift`. The profile
only changes the movement pattern and minimum gates; the sensor mounting angle
does not need to be entered.

Each completed ascent prints:

```text
REP 01 | time 1.20 s | displacement 0.650 m | average speed 0.542 m/s | peak speed 0.910 m/s
```

The generated files are stored locally:

- `beast_repetitions.json` contains accepted repetition measurements.
- `outputs/beast_tracker/Beast Workout.xlsx` contains the formatted Excel log.
- `outputs/recordings/*.jsonl` contains automatic raw sensor recordings.
- `outputs/analysis/*.html` contains optional interactive movement reports.

These runtime files are ignored by Git so every machine starts with a clean training log.

## How repetition detection works

The packet is decoded as a 16-bit sequence number, the Beast `x,y,w,z`
world-to-body quaternion, and XYZ acceleration. The quaternion is normalized
and inverted so every acceleration sample can be rotated from the sensor's
local axes onto world Z. Gravity is then removed using the stationary
calibration. This makes calibration and movement tracking independent of the
sensor's fixed mounting angle.

The packet sequence supplies the integration clock. `47.6 Hz` is only the
startup fallback. During calibration and movement, the tracker estimates the
actual rate from overlapping multi-second sequence-versus-host-time windows.
The estimate is bounded to `43–52 Hz`, changes slowly, and is saved with a
confidence label. Individual Bluetooth callback intervals are deliberately
ignored because Windows may deliver packets in bursts.

The detector first removes isolated spikes with a short Hampel filter, then
smooths world-up acceleration with a causal 5 Hz Butterworth filter.
Acceleration is still the sensor input, but the detector integrates it into
velocity and uses velocity to decide what the bar is doing.

The detector follows these main states:

```text
REST -> UP -> TOP/REP -> DOWN -> BOTTOM -> UP
```

- Four sustained acceleration samples plus at least `0.02 m/s` provisional
  velocity start movement.
- Negative acceleration alone does not mean the bar is moving down. It first
  means the upward-moving bar is slowing down.
- The top is detected when upward velocity returns close to zero after a clear
  velocity peak and deceleration.
- After a positive velocity peak and three braking samples, a local velocity
  minimum is retained when velocity falls by at least 70%. A following fused
  orientation-and-propulsion lobe can close the previous repetition at that
  minimum, remove drift from that lobe only, and restart velocity from zero.
- If integration drift hides that zero-velocity top, a completed movement can
  also be recovered after confirmed rest, a clear
  propulsion-and-braking shape, at least 10° of orientation change, valid
  profile metrics, and no missing packets. The recovered repetition is counted
  but marked `recovered top` in the console, HTML report, JSON history, and
  Excel quality column.
- Orientation activity is measured relative to a changing local baseline band.
  Three sustained samples above the band start a possible region. Prominence,
  excess area, acceleration propulsion and braking must confirm it. Several
  angle peaks remain inside one region, and orientation alone never counts a
  repetition.
- The start and top velocities are then forced to zero, linear integration
  drift is removed, and velocity and distance are recalculated.
- Downward movement is ignored for repetition metrics.
- Downward movement only re-arms the next bench or squat repetition.
- Rest requires 0.3 seconds of low acceleration variation and less than 4° of
  sensor rotation. The measured gravity value may be different from `1.0 g`.
- The gravity baseline is relearned only during confirmed rest, and velocity is
  forced back to zero there.
- An invalid movement can re-synchronize at a later clean fused region instead
  of suppressing every following continuous repetition. A packet gap still
  invalidates the crossing candidate and requires a new clean region or rest.

Very small velocity bumps, distance, duration, and incomplete movement shapes
are checked after a candidate is found. Rejected candidates print a direct
reason in diagnostic mode. For bench press, corrected distance from
`0.03–0.08 m` counts but is explicitly marked `short distance`; values below
`0.03 m` remain rejected.

## Replay a recording

Replay applies the current detector to the exact saved packets without changing
the workout history:

```powershell
.\run.ps1 replay .\outputs\recordings\bench-20260718-131804.jsonl --exercise bench --diagnostic
```

Use the actual filename shown when recording starts. Angle brackets such as
`<recording>` are documentation placeholders and must not be typed literally.

If `--exercise` is omitted during replay or analysis, the profile saved in new
recording metadata is used. A command-line profile always wins. Older
recordings without a profile use `generic`.

## Interactive movement graph

Analysis always decodes the saved `packet_hex` bytes again with the current
algorithm. It never trusts velocity or state fields calculated by an older
version.

```powershell
.\run.ps1 analyze .\outputs\recordings\bench-20260718-131804.jsonl --exercise bench --expected-reps 5
```

Add `--open` to open the result automatically. The self-contained HTML file
works offline and includes synchronized zoomable panels for acceleration,
velocity, displacement, teal rest confidence, orange orientation change,
the adaptive orientation band and threshold, and estimated sample rate. It
also shows shaded orientation regions, provisional velocity minima, recovered
tops, state backgrounds, event markers, packet gaps, quality flags, evidence,
resynchronization reasons, and a horizontally scrollable candidate table.
Report generation is separate from the live Bluetooth loop, so it cannot slow
sensor reading.

## Live local dashboard

The Streamlit dashboard follows the growing JSONL recording and reprocesses its
raw `packet_hex` data with the current detector. It runs separately from the
Bluetooth process, so drawing and browser refreshes cannot delay sensor
notifications.

Open the dashboard in one PowerShell window:

```powershell
.\run.ps1 dashboard --exercise bench
```

Streamlit opens `http://localhost:8501`. Then run the sensor in a second
PowerShell window:

```powershell
.\run.ps1 --exercise bench
```

The dashboard automatically switches to the new recording. It updates the
acceleration, velocity, distance, rest, adaptive orientation, and sample-rate
graphs; shows accepted and rejected movement markers; and keeps a scrollable
candidate table. The sidebar can pause the display, select an older recording,
override its exercise profile, change the refresh interval, and choose how much
recent history is visible.

To follow one specific recording instead:

```powershell
.\run.ps1 dashboard .\outputs\recordings\beast-20260718-211135.jsonl --exercise bench
```

Use `--port 8502` if port 8501 is already occupied. A live recording is flushed
to disk in small batches, so the browser will normally trail the sensor by
about one second.

For the five-repetition bench acceptance recording:

1. Keep the attached sensor still for at least five seconds.
2. Perform exactly five continuous repetitions.
3. Keep the sensor still for at least five more seconds.
4. Stop recording and run `analyze` with `--expected-reps 5`.

## Optional sensor diagnostics

`probe_imu_characteristics.py` performs a read-only inspection of the sensor,
lists its GATT services and readable values, and measures packet sequence rate,
missing samples, bursty host-notification timing, and deviation from the
tracker's `47.6 Hz` fallback rate:

```powershell
.\.venv\Scripts\python.exe .\probe_imu_characteristics.py --duration 12
```

The inspected Beast unit does not expose a readable sample-rate setting or
pairing PIN characteristic. A phone's normal Bluetooth settings may still ask
for a PIN even though direct BLE communication works without pairing. The
inspection tool never guesses a PIN and never writes to unknown
characteristics.

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
