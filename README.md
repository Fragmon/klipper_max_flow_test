# TMC Flow Test

**Adaptive max-volumetric-flow detection for 3D printer extruders using TMC StallGuard.**

Find your extruder's real maximum flow rate automatically — no test prints, no measuring melted noodles, no eyeballing under-extrusion.

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![Klipper](https://img.shields.io/badge/Klipper-compatible-green.svg)](https://www.klipper3d.org/)

Plugin by **Steven (Fragmon) — Crydteam**
[![YouTube](https://img.shields.io/badge/YouTube-@crydteamprinting-red?logo=youtube)](https://www.youtube.com/@crydteamprinting)

---

## What it does

The plugin uses the TMC driver's built-in **StallGuard** load-sensing feature to detect when the extruder motor is approaching slip. It runs an automatic three-phase bracket-bisection test:

1. **Coarse sweep** — increase flow in big steps until StallGuard detects load approaching the limit
2. **Bisection** — narrow the bracket by halving until the safe value is known to ±1 mm³/s
3. **Verification** — confirm the safe value with extra repetitions and report a stability metric

Output is a CSV with raw data and an interactive HTML report:

```
========== FINAL RESULT ==========
Test mode: CS
Maximum safe volumetric flow: 57.0 mm³/s
Verification quality: excellent (very stable) (CV = 1.2%)
----------------------------------
Slicer recommendation:
  Conservative (80%): 45.6 mm³/s   ← recommended
  Aggressive (90%):   51.3 mm³/s   ← only with safety margin
==================================
```

---

## Requirements

- Klipper or Kalico
- TMC stepper driver on the extruder with StallGuard support:

| Driver  | StallGuard | Tested  |
| ------- | ---------- | ------- |
| TMC2240 | SG4        | ✅ yes  |
| TMC2209 | SG4        | ✅ yes  |
| TMC5160 | SG2        | fallback |
| TMC2130 / TMC2208 / TMC2226 / TMC2660 | SG2 | fallback |

- StealthChop must be the active chopper mode (sensorless homing requirement)
- Hotend at print temperature

---

## Installation

```bash
cd ~
git clone https://github.com/Fragmon/klipper_max_flow_test.git
ln -s ~/tmc_flow_test/tmc_flow_test.py ~/klipper/klippy/extras/tmc_flow_test.py
```

Add the [configuration](#configuration) to your `printer.cfg`, then:

```
FIRMWARE_RESTART
```

---

## Quick start

```
M104 S230
M109 S230
TMC_FLOW_FIND_MAX
```

That's it. The plugin auto-detects whether your driver has CoolStep enabled and picks the right test mode automatically.

Test takes ~10 minutes by default. CSV and HTML report are saved to `~/printer_data/config/Flowtest/`.

---

## How it works

The plugin reads StallGuard (`SG_RESULT` / `SG4_RESULT`) and CoolStep current scale (`CS_ACTUAL`) at 20 Hz during extrusion. It detects slip via several triggers:

**SG-only mode** (when `driver_SEMIN: 0`):
- SG abnormal jump: actual increase > 2× expected AND > +15
- SG plateau over 2 steps: cumulative rise < 0.5× expected (motor turning but no longer pushing more filament)

**CoolStep + SG mode** (when `driver_SEMIN > 0`):
- CS_ACTUAL jumps up ≥+5: sudden load increase
- CS_ACTUAL leaves regulation range: approaching slip
- CS_ACTUAL drops sharply: motor lost load contact (hard stall)
- SG abnormal jump: backup detection

Both modes use median + IQR statistics over 5 repetitions per measurement (configurable) to filter out single-cycle noise. The first run is excluded as warmup if it deviates more than 10% from the rest (filament pressure buildup).

---

## Configuration

### 1. TMC driver

Pick the variant matching your driver and CoolStep preference. **Run the test with the same settings you use for actual printing** — don't toggle CoolStep just for the test.

#### TMC2240, CoolStep enabled (klipper_tmc_autotune default)

```ini
[tmc2240 extruder]
cs_pin: PA15
spi_bus: spi4
spi_speed: 2000000
rref: 12300
run_current: 0.85
hold_current: 0.6
interpolate: false
stealthchop_threshold: 999999
coolstep_threshold: 0.5
driver_SEMIN: 5
driver_SEMAX: 2
driver_SEUP: 2
driver_SEDN: 1
driver_SEIMIN: 1
```

#### TMC2240, CoolStep disabled

```ini
[tmc2240 extruder]
cs_pin: PA15
spi_bus: spi4
spi_speed: 2000000
rref: 12300
run_current: 0.85
hold_current: 0.6
interpolate: false
stealthchop_threshold: 999999
coolstep_threshold: 0.5
driver_SEMIN: 0
```

#### TMC2209, CoolStep enabled

```ini
[tmc2209 extruder]
uart_pin: PB12
run_current: 0.85
hold_current: 0.6
interpolate: false
stealthchop_threshold: 999999
coolstep_threshold: 0.5
driver_SEMIN: 5
driver_SEMAX: 2
driver_SEUP: 2
driver_SEDN: 1
driver_SEIMIN: 1
```

#### TMC2209, CoolStep disabled

```ini
[tmc2209 extruder]
uart_pin: PB12
run_current: 0.85
hold_current: 0.6
interpolate: false
stealthchop_threshold: 999999
coolstep_threshold: 0.5
driver_SEMIN: 0
```

### 2. StallGuard threshold setup

Some StallGuard parameters can't be set directly in the `[tmcXXXX]` section — they need a `delayed_gcode`:

#### For TMC2240

```ini
[delayed_gcode setup_extruder_sg]
initial_duration: 2.0
gcode:
    SET_TMC_FIELD STEPPER=extruder FIELD=sg4_thrs VALUE=80
    SET_TMC_FIELD STEPPER=extruder FIELD=sg4_filt_en VALUE=1
```

#### For TMC2209

```ini
[delayed_gcode setup_extruder_sg]
initial_duration: 2.0
gcode:
    SET_TMC_FIELD STEPPER=extruder FIELD=sgthrs VALUE=100
```

(TMC2209 has no filter field.)

### 3. Plugin configuration

```ini
[tmc_flow_test]
extruder_stepper: extruder
filament_diameter: 1.75
melt_zone_length: 42

# Optional:
#min_hotend_temp: 180
#output_dir: ~/printer_data/config/Flowtest
```

## Commands

### `TMC_FLOW_FIND_MAX`

Main command. Auto-detects test mode from `driver_SEMIN`:

- `driver_SEMIN: 0` (or missing) → SG-only mode
- `driver_SEMIN > 0` → CoolStep + SG mode

#### Parameters

| Parameter          | Default | Description                                                    |
| ------------------ | ------- | -------------------------------------------------------------- |
| `START`            | 10      | Starting flow rate (mm³/s)                                     |
| `MAX`              | 80      | Maximum flow to attempt (mm³/s) — set higher than expected limit |
| `COARSE_STEP`      | 10      | Phase 1 step size (mm³/s)                                      |
| `MIN_STEP`         | 1       | Bisection precision (mm³/s)                                    |
| `DURATION`         | 5       | Seconds of extrusion per measurement                           |
| `REPEAT`           | 5       | Repetitions per flow value (higher = more accurate, slower)    |
| `VERIFY_REPEATS`   | 5       | Repetitions in Phase 3 (verification)                          |
| `COOLDOWN`         | 60      | Pause between phases (seconds)                                 |
| `PURGE`            | 0       | Optional purge before test (mm of filament)                    |
| `MAX_BISECT_STEPS` | 6       | Maximum bisection iterations                                   |
| `MODE`             | auto    | `auto`, `sg`, or `cs` to override auto-detection               |
| `NO_HTML`          | 0       | Set to 1 to skip HTML report (CSV only)                        |
| `SKIP_TMC_CHECK`   | 0       | Set to 1 to bypass config validation                           |

### `TMC_FLOW_STATUS`

Diagnostic. Shows live SG/CS values and tells you which test mode would be picked for your config.

### `TMC_FLOW_FIND_MAX_SG` / `TMC_FLOW_FIND_MAX_CS`

Force a specific mode. Equivalent to `TMC_FLOW_FIND_MAX MODE=sg` / `MODE=cs`.

The config check will fail if the forced mode doesn't match your `driver_SEMIN` setting — by design. Use these only for testing or if you know what you're doing.

---

## Examples

```
# Standard test (10–80 mm³/s, ~10 minutes)
TMC_FLOW_FIND_MAX

# Higher flow range for fast extruders
TMC_FLOW_FIND_MAX MAX=120 START=20

# Quicker, less accurate
TMC_FLOW_FIND_MAX REPEAT=3 VERIFY_REPEATS=3 COOLDOWN=30

# More accurate (longer)
TMC_FLOW_FIND_MAX REPEAT=10 DURATION=8 VERIFY_REPEATS=10

# Diagnostic check
TMC_FLOW_STATUS
```

---

## Output

Files are saved to `~/printer_data/config/Flowtest/` (configurable):

```
tmc_flow_<mode>_YYYY-MM-DD_HH-MM-SS.csv     ← raw data
tmc_flow_<mode>_YYYY-MM-DD_HH-MM-SS.html    ← interactive report
```

The HTML report renders in any browser and includes:

- **SG vs. flow chart** with median + IQR (P25/P75) and average
- **CS_ACTUAL vs. flow chart** (CoolStep mode only)
- **Phase markers** — vertical dashed lines at Coarse → Bisection → Verify transitions
- **Data table** with inter-run consistency (CV) for each measurement
- **Stop reason** (which trigger fired and at which flow)

---

## About the two modes

Both modes detect filament slip — but they look at different aspects of motor behaviour. Neither is "better"; they're for different setups.

### CoolStep enabled (`driver_SEMIN > 0`)

CoolStep dynamically adjusts motor current based on load. Lower current at idle/low-flow saves energy and reduces motor heat. The driver ramps current back up when load increases.

This is what `klipper_tmc_autotune` enables by default for extruders.

The test detects slip via CS_ACTUAL transitions (current scale changes) and SG as backup.

### CoolStep disabled (`driver_SEMIN = 0`)

The motor runs at constant `IRUN` current at all times. More heat, more energy used, but maximum torque is always available — no waiting for CoolStep to ramp up.

Used by setups where motor torque reserve matters more than energy saving, or by users who prefer constant-current behaviour.

The test detects slip via SG signal patterns only.

### Don't switch modes just for the test

Configure your driver the way you always print, then run `TMC_FLOW_FIND_MAX`. The plugin auto-detects and picks the right mode. Switching CoolStep on/off just for the test would give you a number that doesn't apply during normal prints.

---

## Troubleshooting

### "sg4_thrs is 0. StallGuard trigger inactive."

The `delayed_gcode setup_extruder_sg` block is missing or didn't run. Check that it's in your `printer.cfg` and run `FIRMWARE_RESTART`.

For a quick test, set it manually in the console:

```
SET_TMC_FIELD STEPPER=extruder FIELD=sg4_thrs VALUE=80
SET_TMC_FIELD STEPPER=extruder FIELD=sg4_filt_en VALUE=1
```

(For TMC2209: `FIELD=sgthrs VALUE=100` and skip the filter line.)

### "SG median = n/a" during the test

SG values aren't being read. Make sure you're running the latest plugin version which reads SG/CS directly from the driver registers (not via `get_status()`).

### Reached MAX without trigger

Your extruder is faster than the default `MAX=80`. Try:

```
TMC_FLOW_FIND_MAX MAX=120
```

### Trigger fires immediately at START

`START` is too high — the test triggered on the very first step. Lower it:

```
TMC_FLOW_FIND_MAX START=5 COARSE_STEP=5
```

### Test result varies between runs

- Increase `REPEAT` and `VERIFY_REPEATS` (e.g. 10 instead of 5)
- Increase `DURATION` (e.g. 8 s)
- Increase `COOLDOWN` between phases (e.g. 90 s) to fully release hotend pressure
- Make sure the hotend is at exact target temp (`M109 S230`)
- Check that the filament feed path is clean and consistent

---

## How is this different from a volumetric flow test print?

Traditional flow tests print towers or stepped lines at increasing flow, then you visually identify where under-extrusion starts. That works but is subjective, slow, and depends on filament cooling, layer adhesion, and your eyes.

This plugin measures the motor itself: when the extruder gear can't keep up with the demanded flow, the motor's StallGuard signal changes characteristically. That's the actual mechanical limit, independent of slicer settings, layer height, or how the printed sample looks.

Result: **direct measurement of your hotend's melt-zone capacity** in roughly 10 minutes, with a number you can paste straight into your slicer.

---

## Credits & License

Plugin by Steven (Fragmon) — Crydteam.

YouTube: [@crydteamprinting](https://www.youtube.com/@crydteamprinting)

Released under the GNU General Public License v3.0. See [LICENSE](LICENSE) for details.

Inspired by Klipper's StallGuard implementation and the work of the [klipper_tmc_autotune](https://github.com/andrewmcgr/klipper_tmc_autotune) project.
