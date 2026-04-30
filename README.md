# TMC Flow Test
**Adaptive max-volumetric-flow detection for 3D printer extruders using TMC StallGuard.**

Find your extruder's real max flow rate automatically — no test prints, no measuring melted noodles.

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![Klipper](https://img.shields.io/badge/Klipper-compatible-green.svg)](https://www.klipper3d.org/)

Plugin by **Steven (Fragmon) — Crydteam**
[![YouTube](https://img.shields.io/badge/YouTube-@crydteamprinting-red?logo=youtube)](https://www.youtube.com/@crydteamprinting)

<p align="center">
  <img src="images/results.png" alt="TMC Flow Test HTML report" width="700">
</p>

---

## What it does

The plugin reads the TMC driver's **StallGuard** load signal during extrusion to detect when the motor is approaching slip. It runs a three-phase test:

1. **Coarse sweep** — flow rises in big steps until StallGuard sees the limit approach
2. **Bisection** — the safe value is narrowed to ±1 mm³/s; borderline measurements are auto-re-tested
3. **Verification** — final value is confirmed with extra repeats and a stability metric; if verify fails, the test drops back into bisection with a tighter bracket

Output: a CSV with raw data and an interactive HTML report with a **decision trail** (every trigger that fired, why, and which value defined the result).

> **Important for TMC2240 users**: The plugin runs the TMC2240 in the **SG2/SpreadCycle path** (same as TMC5160). The TMC2240's SG4/StealthChop path delivers ~50 % less peak torque — it's intended for sensorless homing, not high-flow extrusion. See [TMC2240 config](#tmc2240) below.

---

## Quick start

1. **Install** (one-liner):
   ```bash
   cd ~/klipper/klippy/extras && \
   wget -O tmc_flow_test.py https://raw.githubusercontent.com/Fragmon/tmc_flow_test/main/tmc_flow_test.py
   ```
2. **Configure** your `[tmcXXXX extruder]` and add a `[tmc_flow_test]` section (see [Configuration](#configuration) below).
3. `FIRMWARE_RESTART`.
4. **Heat your hotend** to printing temperature, load filament.
5. Run:
   ```
   TMC_FLOW_FIND_MAX MAX=150 START=20
   ```

Test takes ~10 minutes (+30–60 s if borderline measurements need re-testing). CSV and HTML report land in `~/printer_data/config/Flowtest/`.

---

## Before you run: SG range check

The plugin only works well when StallGuard produces a **usable signal range**. Run `TMC_FLOW_STATUS` first to see the current SG value, then run a short test and watch the SG values printed in the Klipper console.

| Driver | Sensitivity field | Healthy SG range across the sweep |
|---|---|---|
| TMC5160 / TMC2240 / TMC2130 (SG2) | `driver_SGT` | low load: ~400–600, near slip: ~50–150 |
| TMC2209 (SG4) | `driver_SGTHRS` | low load: ~300–500, near slip: ~50–150 |

You want **at least 200 raw SG units** of dynamic range between low-load and high-load, with neither end saturated (0 or 1023).

**SG too low** (median <50, often saturating at 0):
- SG2 drivers (TMC5160 / TMC2240 / TMC2130): raise `driver_SGT` (e.g. +5 to +10)
- TMC2209: lower `driver_SGTHRS` (e.g. by 20)

**SG too high** (often saturating at 1023): opposite of above.

A tested reference for **TMC5160 + Sherpa Mini**: `driver_SGT: 15` gives SG ≈ 540 at low flow, ≈ 90 near slip.
A tested reference for **TMC2240 + Sherpa Mini**: `driver_SGT: 5` gives a similar useful range.

---

## Configuration

### TMC driver section

Pick the variant matching your driver. **Run the test with the same chopper-mode and current you use for printing.**

> ⚠️ Lines marked `# ADAPT` depend on your hardware. The other lines are required by the plugin.

#### TMC5160 (and TMC2130 / TMC2660)

```ini
[tmc5160 extruder]
cs_pin: PA15                     # ADAPT
spi_bus: spi4                    # ADAPT
run_current: 0.85                # ADAPT
hold_current: 0.6                # ADAPT
sense_resistor: 0.075            # ADAPT
interpolate: false
# DO NOT add stealthchop_threshold — see "Chopper mode" notes below
coolstep_threshold: 0.5          # required for StallGuard reads (NOT for CoolStep)
driver_SGT: 15                   # SG2 sensitivity, signed -64..63 (higher = less sensitive)
driver_SFILT: 1                  # SG2 filter (recommended for extruders)
```

#### TMC2240

The plugin runs TMC2240 in **SG2/SpreadCycle mode** — same path as TMC5160. This gives ~50 % more peak torque than the SG4/StealthChop path.

```ini
[tmc2240 extruder]
cs_pin: PA15                     # ADAPT
spi_bus: spi4                    # ADAPT
rref: 12300                      # ADAPT (12000-60000 depending on hardware)
run_current: 0.85                # ADAPT
hold_current: 0.6                # ADAPT
interpolate: false
# DO NOT add stealthchop_threshold — SG2 needs SpreadCycle
coolstep_threshold: 0.5          # required for StallGuard reads (NOT for CoolStep)
driver_SGT: 5                    # SG2 sensitivity, signed -64..63 (higher = less sensitive)
driver_SFILT: 1                  # SG2 filter (recommended for extruders)
```

> **Switching from SG4 to SG2 on TMC2240?** If you previously ran the TMC2240 with `stealthchop_threshold: 999999` and a `SET_TMC_FIELD ... sg4_thrs ...` macro for sensorless homing, you'll need to disable that for the test. The plugin will detect StealthChop in `TMC_FLOW_STATUS` and tell you what to remove.

#### TMC2209

```ini
[tmc2209 extruder]
uart_pin: PB12                   # ADAPT
run_current: 0.85                # ADAPT
hold_current: 0.6                # ADAPT
sense_resistor: 0.110            # ADAPT
interpolate: false
stealthchop_threshold: 999999    # SG4 requires StealthChop
coolstep_threshold: 0.5          # required for StallGuard reads
driver_SGTHRS: 100               # SG4 sensitivity (0-255, higher = more sensitive)
```

### Plugin section

```ini
[tmc_flow_test]
extruder_stepper: extruder       # ADAPT: stepper section name
filament_diameter: 1.75          # ADAPT: 1.75 or 2.85
melt_zone_length: 42             # ADAPT: hotend melt-zone length (Sherpa Mini ~42, V6 ~12, Volcano ~21)

# Optional:
#min_hotend_temp: 180            # safety floor, default 180
#output_dir: ~/printer_data/config/Flowtest
```

### CoolStep — what about it?

CoolStep is the TMC feature that dynamically reduces motor current under low load. **The plugin does not require CoolStep to be on or off** — slip detection uses StallGuard signal directly.

You can keep your existing CoolStep configuration in the `[tmc<NNNN>]` section. The plugin prints a notice at test start indicating whether CoolStep is active.

For the **most conservative max-flow result**, set `driver_SEMIN: 0` (CoolStep off). With CoolStep off, the motor runs at constant `run_current` — the test then reflects what the motor can sustain at that exact current, which matches what happens during high-load printing.

If you leave CoolStep on, the test still works, but CS-driven current changes during the sweep can dampen the StallGuard signal slightly and produce a marginally higher (less conservative) result.

---

## Chopper mode (important)

Each TMC chip family supports StallGuard only in one specific chopper mode:

| Driver | StallGuard variant | Required mode |
|---|---|---|
| TMC5160 / TMC2130 / TMC2660 | SG2 | **SpreadCycle** (Klipper default — **don't add `stealthchop_threshold`**) |
| TMC2240 | SG2 *(used by this plugin)* | **SpreadCycle** (don't add `stealthchop_threshold`) |
| TMC2209 | SG4 | StealthChop (`stealthchop_threshold: 999999`) |

> **`stealthchop_threshold: 0` is NOT the same as "no line".** It enables StealthChop with threshold 0, which breaks SG2. For SG2 drivers, **remove the line entirely**.

If you're not sure what mode you're in, run `TMC_FLOW_STATUS` — the plugin checks the configuration and tells you what's wrong. Or `DUMP_TMC STEPPER=extruder` and check `en_pwm_mode` (1 = StealthChop) and `tpwmthrs` (1048575 = pure SpreadCycle).

---

## How it works

The plugin samples StallGuard at 20 Hz during each measurement step (5 repeats by default) and tracks median, IQR (P25–P75), and run-to-run CV per step. Slip detection uses **multiple independent triggers** that look for different signatures:

- **SG signal patterns** — snap-back, over-jump, plateau
- **Run-to-run variance** — CV spike, CV jump, rising trend, vs coarse-baseline
- **Sample distribution** — IQR widening, IQR vs coarse-baseline, IQR absolute
- **Per-run analysis** — single-run outlier detection, SG max spike for decoupling

Each trigger fires under tighter conditions in **bisection / verify** than in coarse, so the coarse phase stays noise-resistant while the final result is accurate to ±1 mm³/s.

The HTML report's **decision-trail panel** lists every trigger event with the metrics that caused it, so you can see exactly why the plugin chose the value it did.

### Per-driver tuning

Each driver family has its own `TriggerProfile` in the source code with all detection thresholds. The TMC5160 profile is the validated production baseline. TMC2240 (SG2 path) inherits the same values since the hardware path is identical. TMC2209 has its own profile for the SG4 path. To tune sensitivity for one driver without affecting the others, edit only its profile class.

---

## Commands

### `TMC_FLOW_FIND_MAX`

Run the StallGuard-based flow test (Coarse → Bisection → Verification).

| Parameter | Default | Description |
|---|---|---|
| `START` | 10 | Starting flow (mm³/s) |
| `MAX` | 80 | Upper search bound |
| `COARSE_STEP` | 10 | Coarse sweep step size |
| `MIN_STEP` | 1 | Bisection precision |
| `DURATION` | 5 | Seconds per measurement |
| `REPEAT` | 5 | Repetitions per measurement |
| `VERIFY_REPEATS` | 5 | Repetitions in the verify phase |
| `COOLDOWN` | 15 | Pause between phases (seconds) |
| `PURGE` | 0 | Purge length (mm) before test |
| `MAX_BISECT_STEPS` | 6 | Max bisection iterations |
| `NO_HTML` | 0 | Set 1 to skip HTML report |
| `SKIP_TMC_CHECK` | 0 | Set 1 to bypass config validation |

### `TMC_FLOW_STATUS`

Diagnostic check: reads current SG value, verifies driver, chopper mode, and StallGuard threshold. **Run this before the first test.**

| Parameter | Default | Description |
|---|---|---|
| `ACTIVATE` | 1 | Briefly run motor (1 mm extrusion) so SG can be read |

---

## Examples

```
# Standard test, fast extruders
TMC_FLOW_FIND_MAX MAX=150 START=20

# Quicker, less accurate
TMC_FLOW_FIND_MAX REPEAT=3 VERIFY_REPEATS=3 COOLDOWN=10

# More accurate (longer)
TMC_FLOW_FIND_MAX REPEAT=10 DURATION=8 VERIFY_REPEATS=10

# Diagnostic check
TMC_FLOW_STATUS
```

---

## Output

Files saved to `~/printer_data/config/Flowtest/`:

```
tmc_flow_YYYY-MM-DD_HH-MM-SS.csv     ← raw data
tmc_flow_YYYY-MM-DD_HH-MM-SS.html    ← interactive report
```

The HTML report includes:
- **Result panel** — max safe flow, slicer recommendations (80 % / 90 %)
- **Decision trail** — every trigger event with the "healthy ranges" that defined what was considered normal
- **SG vs flow chart** with median + IQR, phase markers, and trigger annotations marking exactly where slip was detected
- **TMC settings snapshot** at test start (collapsible) for reproducible documentation

The CSV header includes the same TMC settings block for paper-trail purposes.

---

## Troubleshooting

**`TMC_FLOW_STATUS` reports "StallGuard2 needs SpreadCycle"** *(SG2 drivers — TMC5160, TMC2130, TMC2240)* —
Remove the `stealthchop_threshold:` line entirely from your TMC section, `FIRMWARE_RESTART`. For TMC2240: also remove any `[delayed_gcode]` block that sets `sg4_thrs` or `sg4_filt_en` — they're not needed in SG2 mode.

**`TMC_FLOW_STATUS` reports "StealthChop not active"** *(TMC2209 only)* —
Add `stealthchop_threshold: 999999` to your `[tmc2209 extruder]` section, `FIRMWARE_RESTART`.

**`TMC_FLOW_STATUS` reports "SGTHRS is 0"** *(TMC2209 only)* —
Add `driver_SGTHRS: 100` to your `[tmc2209 extruder]` section.

**SG values too low / saturate at 0** —
Sensitivity is too high. Raise `driver_SGT` (SG2 drivers) or lower `driver_SGTHRS` (TMC2209).

**SG values too high / saturate at 1023** —
Sensitivity is too low. Lower `driver_SGT` (SG2 drivers) or raise `driver_SGTHRS` (TMC2209).

**Test reaches MAX without trigger** —
Either your hotend really can flow that fast (raise MAX), or SG sensitivity is too low (see "SG too high" above).

**Trigger fires immediately at START** —
SG sensitivity is too high (see "SG too low"), or your hotend isn't fully heated.

**TMC2240 results much lower than expected (<70 mm³/s on a fast extruder)** —
Check that you're running in **SpreadCycle/SG2** mode, not StealthChop/SG4. The SG4 path of the TMC2240 reduces peak torque by ~50 %. `TMC_FLOW_STATUS` will tell you which mode is active. Remove `stealthchop_threshold` from your `[tmc2240]` section if present.

**Result varies between runs by more than 5 mm³/s** —
Check filament consistency, hotend temperature stability, possible filament path obstructions. Increase `COOLDOWN` between phases (e.g. 30 s).

**"CoolStep is active" notice** —
The plugin works fine with CoolStep on, but for the most conservative result set `driver_SEMIN: 0` to disable CoolStep during testing. CoolStep can dampen StallGuard signal during high-load steps.

---

## How is this different from a flow tower print?

| Flow tower print | TMC Flow Test |
|---|---|
| Visually inspect for under-extrusion | Reads motor's actual load signal |
| Subjective threshold | Objective StallGuard data |
| Wastes filament + ~1 hour | Minimal filament, ~10 minutes |
| One value per test | Full statistical profile + decision trail |
| Tells you when extrusion *looks* bad | Tells you when the *motor is starting to slip* |

Both methods measure different things — the motor can slip before extrusion looks bad (under-extrusion), or extrusion can look bad before the motor slips (cooling / pressure issues). For maximum-flow tuning where torque is the limit, this plugin is the more direct measurement.

---

## Credits & License

Released under the **GNU GPL v3.0**. See [LICENSE](LICENSE).

Inspired by Klipper's StallGuard implementation and [klipper_tmc_autotune](https://github.com/andrewmcgr/klipper_tmc_autotune).

Plugin author: **Steven (Fragmon) — Crydteam** · [YouTube: @crydteamprinting](https://www.youtube.com/@crydteamprinting)

Contributions and feedback welcome — please open an issue or PR.
