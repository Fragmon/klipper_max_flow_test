# TMC Flow Test

**Adaptive max-volumetric-flow detection for 3D printer extruders using TMC StallGuard.**

Find your extruder's real maximum flow rate automatically — no test prints, no measuring melted noodles. 

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

- **StealthChop must be the active chopper mode** for the test — see [the StealthChop section](#-important-stealthchop-required-for-stallguard) below
- Hotend at print temperature

---

## Installation

```bash
cd ~
git clone https://github.com/Fragmon/klipper_max_flow_test.git
ln -s ~/klipper_max_flow_test/tmc_flow_test.py ~/klipper/klippy/extras/tmc_flow_test.py
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

> ⚠️ **Adapt before pasting:** Lines marked with `# ADAPT` depend on your specific board, wiring and motor. Don't copy these blocks blindly — at minimum, set the right pins and current for your hardware. The other lines (StallGuard / CoolStep settings) are required by the plugin and should be kept as shown.

#### TMC2240, CoolStep enabled (klipper_tmc_autotune default)

```ini
[tmc2240 extruder]
cs_pin: PA15                     # ADAPT: SPI chip-select pin on your board
spi_bus: spi4                    # ADAPT: SPI bus name on your board
spi_speed: 2000000               # ADAPT only if your board needs it
rref: 12300                      # ADAPT: external reference resistor on your TMC2240 module (Ω)
run_current: 0.85                # ADAPT: motor RMS current — match your stepper datasheet
hold_current: 0.6                # ADAPT: typically 60–70% of run_current
interpolate: false               # required for clean SG readings (true also works but adds noise)
stealthchop_threshold: 999999    # required: StallGuard needs StealthChop active at all speeds
coolstep_threshold: 0.5          # required: enables StallGuard reading above this velocity
driver_SEMIN: 5                  # required for CoolStep mode (must be > 0)
driver_SEMAX: 2                  # CoolStep upper threshold
driver_SEUP: 2                   # current increment step
driver_SEDN: 1                   # current decrement step
driver_SEIMIN: 1                 # min current = 1/2 IRUN when CoolStep regulates down
```

#### TMC2240, CoolStep disabled

```ini
[tmc2240 extruder]
cs_pin: PA15                     # ADAPT: SPI chip-select pin on your board
spi_bus: spi4                    # ADAPT: SPI bus name on your board
spi_speed: 2000000               # ADAPT only if your board needs it
rref: 12300                      # ADAPT: external reference resistor on your TMC2240 module (Ω)
run_current: 0.85                # ADAPT: motor RMS current
hold_current: 0.6                # ADAPT: typically 60–70% of run_current
interpolate: false               # required for clean SG readings
stealthchop_threshold: 999999    # required: StallGuard needs StealthChop
coolstep_threshold: 0.5          # required: enables StallGuard reading
driver_SEMIN: 0                  # required for SG-only mode (CoolStep disabled)
```

#### TMC2209, CoolStep enabled

```ini
[tmc2209 extruder]
uart_pin: PB12                   # ADAPT: UART pin on your board (CAN toolheads need 'can0:' prefix)
#tx_pin: PB11                    # ADAPT: only needed on some boards (separate TX/RX)
#uart_address: 3                 # ADAPT: only when multiple TMC2209s share one UART
run_current: 0.85                # ADAPT: motor RMS current
hold_current: 0.6                # ADAPT: typically 60–70% of run_current
sense_resistor: 0.110            # ADAPT: matches your stepstick (typical 0.110 Ω, some boards 0.075 Ω)
interpolate: false               # required for clean SG readings
stealthchop_threshold: 999999    # required: StallGuard needs StealthChop
coolstep_threshold: 0.5          # required: enables StallGuard reading
driver_SEMIN: 5                  # required for CoolStep mode (must be > 0)
driver_SEMAX: 2
driver_SEUP: 2
driver_SEDN: 1
driver_SEIMIN: 1
```

#### TMC2209, CoolStep disabled

```ini
[tmc2209 extruder]
uart_pin: PB12                   # ADAPT: UART pin on your board
#tx_pin: PB11                    # ADAPT: only on some boards
#uart_address: 3                 # ADAPT: only when sharing UART
run_current: 0.85                # ADAPT: motor RMS current
hold_current: 0.6                # ADAPT
sense_resistor: 0.110            # ADAPT: matches your stepstick
interpolate: false               # required
stealthchop_threshold: 999999    # required
coolstep_threshold: 0.5          # required
driver_SEMIN: 0                  # required for SG-only mode
```

#### Pin reference: where to find your values

If you're not sure what pins to use:
- **Existing config**: if you already have a `[tmc2240 extruder]` or `[tmc2209 extruder]` block in your `printer.cfg`, **keep your existing pins/wiring** and just add/update the StallGuard-related lines (`stealthchop_threshold`, `coolstep_threshold`, `driver_SEMIN`, ...).
- **Board manual**: check your control board's documentation (BTT, Mellow, MKS, etc.) for the correct `cs_pin`/`uart_pin`/`spi_bus` values.
- **CAN toolheads**: pins prefix with `can0:` — for example `uart_pin: can0:PA15`.

### 2. StallGuard threshold setup

Some StallGuard parameters can't be set directly in the `[tmcXXXX]` section — they need a `delayed_gcode` that runs after Klipper boot:

#### For TMC2240

```ini
[delayed_gcode setup_extruder_sg]
initial_duration: 2.0
gcode:
    SET_TMC_FIELD STEPPER=extruder FIELD=sg4_thrs VALUE=80
    SET_TMC_FIELD STEPPER=extruder FIELD=sg4_filt_en VALUE=1
```

> The `VALUE=80` for `sg4_thrs` is a starting point for typical pancake-style steppers. Higher = more sensitive. Range 0–255. If the test triggers immediately at low flow, lower this value (e.g. 60). If it never triggers, raise it.

#### For TMC2209

```ini
[delayed_gcode setup_extruder_sg]
initial_duration: 2.0
gcode:
    SET_TMC_FIELD STEPPER=extruder FIELD=sgthrs VALUE=100
```

> The `VALUE=100` for `sgthrs` is a starting point. Higher = more sensitive. Range 0–255. TMC2209 has no filter field (unlike TMC2240).

### 3. Plugin configuration

```ini
[tmc_flow_test]
extruder_stepper: extruder       # ADAPT: name of your extruder stepper section
filament_diameter: 1.75          # ADAPT: 1.75 or 2.85 / 3.0 for direct-drive types
melt_zone_length: 42             # ADAPT: hotend melt-zone length in mm (Sherpa Mini ~42, V6 ~12, Volcano ~21)

# Optional:
#min_hotend_temp: 180            # safety floor — test refuses to run below this
#output_dir: ~/printer_data/config/Flowtest
```
---
## ⚠️ Important: StealthChop required for StallGuard

This plugin can only work when the extruder driver is in **StealthChop** mode. This is a hardware limitation of the TMC chips, not the plugin:

- **TMC2240 / TMC2209**: use **StallGuard4**, which works only in StealthChop ([source: Trinamic AN-002](https://www.analog.com/en/resources/app-notes/an-002.html))
- **TMC5160 / TMC2130 / TMC2660**: use StallGuard2, which works only in SpreadCycle

Klipper's default for all TMC drivers is **SpreadCycle**. Many printer profiles (Voron, RatRig, etc.) use SpreadCycle on the extruder for higher torque. **In that default state, the plugin won't work** — it will report `SG median = n/a` or fail the config check with `stealthchop_threshold` warnings.

### How do I know which mode I'm in?

Run in the Klipper console:

```
DUMP_TMC STEPPER=extruder
```

Look at the output:

- For **TMC2240**: line `en_pwm_mode=1` means StealthChop, `en_pwm_mode=0` means SpreadCycle
- For **TMC2209**: line `en_spreadCycle=0` means StealthChop, `en_spreadCycle=1` means SpreadCycle
- All drivers: if `tpwmthrs` is mid-range (not 0 and not 0xFFFFF), the driver switches modes based on speed — that's a problem for the test

You can also just run `TMC_FLOW_STATUS` — the plugin checks all of this and reports any issues.

### What if my extruder is on SpreadCycle?

You have three options:

#### Option 1 — Switch your extruder to StealthChop permanently (recommended)

Add to your `[tmc2240 extruder]` or `[tmc2209 extruder]` section in `printer.cfg`:

```ini
stealthchop_threshold: 999999
```

Then `FIRMWARE_RESTART`. The `999999` value means "always use StealthChop, regardless of speed".

**Trade-offs to be aware of:**
- StealthChop is quieter and runs cooler at low/medium speeds
- SpreadCycle gives higher peak torque at very high speeds and accelerations
- Some users see [slight pressure-advance differences](https://www.klipper3d.org/TMC_Drivers.html#stallguard-and-stealthchop) when switching modes

For most extruders below ~50 mm³/s, StealthChop works fine and is what `klipper_tmc_autotune` recommends by default. Try it on a calibration print before your next big print.

#### Option 2 — Switch only for the test, then back

If you really want to keep SpreadCycle for printing but still run this test:

```
# Before the test:
SET_TMC_FIELD STEPPER=extruder FIELD=en_pwm_mode VALUE=1   # TMC2240
# or for TMC2209:
SET_TMC_FIELD STEPPER=extruder FIELD=en_spreadCycle VALUE=0

# Run the test:
TMC_FLOW_FIND_MAX

# After the test, revert:
SET_TMC_FIELD STEPPER=extruder FIELD=en_pwm_mode VALUE=0   # TMC2240
# or for TMC2209:
SET_TMC_FIELD STEPPER=extruder FIELD=en_spreadCycle VALUE=1
```

**⚠️ Caveat:** The result will reflect the StealthChop max flow, not your normal SpreadCycle behaviour. Expect the actual SpreadCycle limit to be **5–15% higher** than what the test reports, because SpreadCycle delivers more torque at high speeds. Use the test value as a conservative lower bound.

#### Option 3 — Don't use this plugin

If your extruder absolutely needs SpreadCycle (very high-flow setups, specific motor characteristics), this plugin isn't the right tool. Stick to traditional flow tower prints — they don't depend on chopper mode.

---

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

### "StealthChop is not active. StallGuard needs StealthChop ON."

Your `[tmcXXXX extruder]` section is missing `stealthchop_threshold: 999999` (or it's set to a low value that disables StealthChop at higher speeds). See [the StealthChop section](#-important-stealthchop-required-for-stallguard) for the full explanation and three options to fix it.

Quickest fix: add `stealthchop_threshold: 999999` to your extruder TMC section and `FIRMWARE_RESTART`.

### "Unable to read tmc uart 'extruder' register IFCNT"

UART communication with the TMC2209 isn't working. Not a plugin issue — Klipper can't talk to the driver at all.

- Check `uart_pin` (CAN toolheads need `can0:` prefix, e.g., `uart_pin: can0:PA15`)
- Check `uart_address` if multiple drivers share a UART
- Check sense resistor value (`sense_resistor: 0.110` for typical SilentStepSticks)
- If using `klipper_tmc_autotune`, try commenting out `[autotune_tmc extruder]` temporarily — known conflict on some setups

### "sg4_thrs is 0. StallGuard trigger inactive."

The `delayed_gcode setup_extruder_sg` block is missing or didn't run. Check that it's in your `printer.cfg` and run `FIRMWARE_RESTART`.

For a quick test, set it manually in the console:

```
SET_TMC_FIELD STEPPER=extruder FIELD=sg4_thrs VALUE=80
SET_TMC_FIELD STEPPER=extruder FIELD=sg4_filt_en VALUE=1
```

(For TMC2209: `FIELD=sgthrs VALUE=100` and skip the filter line.)

### "SG median = n/a" during the test

The most common cause: your extruder is in **SpreadCycle** mode, not StealthChop. StallGuard4 (TMC2240/TMC2209) only works in StealthChop. See [the StealthChop section above](#-important-stealthchop-required-for-stallguard) for how to fix this.

Other possible causes:
- Plugin is older than the register-direct-read version — make sure you're on the latest from this repo (the plugin must read SG/CS directly from the driver registers, not via `get_status()`)
- TMC driver isn't responding at all — run `DUMP_TMC STEPPER=extruder` to verify communication

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

---

## Contributing

Issues and pull requests welcome. If you've tested this on a driver not listed above (TMC5160, TMC2130, TMC2226, TMC2660), let me know how it went — I'd love to add confirmed-working markers for those.
