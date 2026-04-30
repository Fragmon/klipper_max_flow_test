# TMC Flow Test
**Adaptive max-volumetric-flow detection for 3D printer extruders using TMC StallGuard.**

Find your extruder's real maximum flow rate automatically — no test prints, no measuring melted noodles. 

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![Klipper](https://img.shields.io/badge/Klipper-compatible-green.svg)](https://www.klipper3d.org/)

Plugin by **Steven (Fragmon) — Crydteam**
[![YouTube](https://img.shields.io/badge/YouTube-@crydteamprinting-red?logo=youtube)](https://www.youtube.com/@crydteamprinting)

<p align="center">
  <img src="images/results.png" alt="TMC Flow Test HTML report" width="700">
</p>

---

## What it does

The plugin uses the TMC driver's built-in **StallGuard** load-sensing feature to detect when the extruder motor is approaching slip. It runs an automatic three-phase bracket-bisection test:

1. **Coarse sweep** — increase flow in big steps until StallGuard detects load approaching the limit
2. **Bisection** — narrow the bracket by halving until the safe value is known to ±1 mm³/s, with automatic re-test of borderline measurements
3. **Verification** — confirm the safe value with extra repetitions and report a stability metric

Output is a CSV with raw data and an interactive HTML report.

Real test on a Sherpa Mini extruder, TMC2240 (StealthChop, run_current 0.85 A):

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

Same printer, TMC5160 swapped in (SpreadCycle, run_current 0.85 A):

```
========== FINAL RESULT ==========
Test mode: CS
Maximum safe volumetric flow: 116.0 mm³/s
Verification quality: excellent (very stable) (CV = 1.1%)
----------------------------------
Slicer recommendation:
  Conservative (80%): 92.8 mm³/s   ← recommended
  Aggressive (90%):  104.4 mm³/s   ← only with safety margin
==================================
```

### What to check before running the test

The plugin only works well when StallGuard produces a **usable signal range** for your hardware. If the `SG_RESULT` values printed during the test sit in the wrong window, the triggers can't distinguish slip from noise. **Run the test once and look at the SG values printed live for each step.**

#### Target SG range

| Driver | Field that shifts the range | Healthy SG range across your test flows | Why |
|---|---|---|---|
| TMC2240 (SG4) | `driver_sg4_thrs` | low load: ~300–500, near slip: ~50–150 | SG4 falls as load rises; needs headroom at low loads so slip is visible |
| TMC2209 (SG4) | `driver_SGTHRS` | similar to TMC2240 | same engine, same direction |
| TMC5160 / 2130 (SG2) | `driver_SGT` | low load: ~400–600, near slip: ~50–150 | SG2 also falls with load but on a 0–1023 scale; SGT shifts the whole curve up/down |

The exact numbers don't have to match — what matters is that you have **at least 200 raw SG units of dynamic range** between low-load and near-slip readings, and that **neither end is saturated** (SG = 0 at high load, or SG = 1023 at low load).

#### What to do if your SG range is off

**SG values too low** (median below ~50 across the whole sweep, or sitting at 0 most of the time):
- TMC5160 / 2130: **raise** `driver_SGT` (it's signed, higher = less sensitive = higher SG_RESULT). Try +5 or +10 increments.
- TMC2240 / 2209: **lower** `driver_sg4_thrs` / `driver_SGTHRS`. Try in 20-unit steps.
- Symptom: false-positive triggers in coarse phase, test stops at very low flow without real slip.

**SG values too high** (median sitting near 1023, or above 800 even at high flows):
- TMC5160 / 2130: **lower** `driver_SGT`.
- TMC2240 / 2209: **raise** `driver_sg4_thrs` / `driver_SGTHRS`.
- Symptom: test reaches `MAX` without triggering, even though the motor obviously struggles.

**SG range looks fine but test still triggers too early:**
- Increase `coolstep_threshold` slightly (e.g. from `0.5` to `1.0`) — gates StallGuard above a minimum velocity, suppresses noise during ramps.
- Enable the SG filter: `driver_SFILT: True` (TMC5160) or `driver_sg4_filt_en: True` (TMC2240). Smooths over 4 full-steps.

**A tested reference for TMC5160** (Sherpa Mini, 0.85 A, PLA at 230 °C): `driver_SGT: 15` produces SG ≈ 540 at 20 mm³/s and SG ≈ 90 at 110 mm³/s. Use this as a starting point.

See [StallGuard tuning](#2-stallguard-tuning) below for the complete tuning workflow.

---

## Requirements

- Klipper or Kalico
- hotend at print temperature
- TMC stepper driver on the extruder with StallGuard support:

| Driver  | StallGuard | Required chopper mode for the test       | Tested |
| ------- | ---------- | ---------------------------------------- | ------ |
| TMC2240 | SG4 / SG2  | StealthChop (SG4) **or** SpreadCycle (SG2) | ✅ tested (previous version) |
| TMC2209 | SG4        | StealthChop only                         | ✅ tested (previous version) |
| TMC5160 | SG2        | SpreadCycle (only mode SG2 works in)     | ✅ tested (actual version) |
| TMC2130 | SG2        | SpreadCycle                              | code path present, untested |
| TMC2660 | SG2        | SpreadCycle                              | code path present, untested |
| TMC2208 / TMC2226 | — | (no StallGuard; not supported)         | not supported |

**The TMC2240 is special**: it has both StallGuard4 AND StallGuard2 on-chip. The plugin works in either mode — see [the chopper mode section](#-important-chopper-mode-required-for-stallguard) below for details. SG2/SpreadCycle is the `klipper_tmc_autotune` default and the more practical choice for printing.

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

Test takes ~10 minutes by default (+30 – 60 s if borderline measurements need re-testing). CSV and HTML report are saved to `~/printer_data/config/Flowtest/`.

---

## How it works

The plugin reads StallGuard (`SG_RESULT` / `SG4_RESULT`) and CoolStep current scale (`CS_ACTUAL`) at 20 Hz during extrusion. Each measurement step runs N repetitions (5 by default), and the plugin tracks median, IQR (P25–P75) and run-to-run variance (CV%) per step.

Slip detection layers several independent triggers — any one fires:

### Always-on triggers (work regardless of mode)

**1. Snap-back.** During normal extrusion the SG signal trends in one direction with rising flow. When the motor decouples (slip), SG snaps sharply back toward its no-load value. The plugin learns the trend direction from the recent steps so this works for both SG2 (SG falls with load) and SG4 (SG often rises with load on extruders). *Coarse phase only — bisection probes flows in arbitrary order so trend baselines aren't meaningful.*

**2. Over-jump.** SG moves in the trend direction, but much faster than the typical step (>2× expected, with a minimum jump of 5 raw units on SG2 / 15 on SG4). Sudden over-acceleration of the load signal — typical SG4 slip pattern. *Coarse phase only.*

**3. CV spike — high variance.** Run-to-run CV jumps to ≥ 10 % AND ≥ 1.5× the recent average. Catches sudden chaotic slip.

**4. CV jump — immediate baseline.** CV reaches ≥ 5 % (coarse) or ≥ 4 % (bisection) AND ≥ 2.5× (coarse) or 2.0× (bisection) the avg of the previous 3 steps. Catches the characteristic CV jump on stable SG2 setups (TMC5160) where overall variance is low but a sudden spike still indicates slip. **Often fires one full step before the snap-back/over-jump triggers do.**

**5. CV rising trend.** CV grows ≥ 1.3× across each of two consecutive steps AND reaches ≥ 5 % (coarse) or ≥ 4 % (bisection). Catches gradual slip onset where each individual step is borderline but the trend is unmistakable.

**6. CV vs coarse-phase baseline (bisection only).** Current CV ≥ 5 % AND ≥ 2.0× the median CV from the COARSE phase. Catches cases where one elevated bisect step has already pulled the immediate baseline up, masking subsequent elevated values. The coarse phase is the slip-free reference.

**7. IQR/spread anomaly — immediate baseline.** The IQR (P75 − P25) widens to ≥ 12 raw units AND ≥ 3× (coarse) or 1.7× (bisection) the recent baseline. Catches "quiet" stalls that occur briefly at the start or end of a move and are absorbed by the median over 5 repetitions, but show up as a wider sample distribution.

**8. IQR vs coarse-phase baseline (bisection only).** IQR ≥ 18 raw units AND ≥ 2.5× the median IQR from the COARSE phase. Same idea as trigger 6 but for spread instead of variance.

**9. IQR absolute (bisection only).** IQR ≥ 25 raw units. A motor near slip should have median, P25 and P75 within a few units of each other; an IQR of 25+ is unmistakable slip regardless of context.

### CoolStep-specific triggers (only when CoolStep regulates `CS_ACTUAL`)

These fire only when CoolStep is genuinely adjusting current (`CS_ACTUAL` varies across steps). If `CS_ACTUAL` stays static at 31 the whole run — common on TMC5160 with low SEMIN — the plugin auto-detects that and falls back to the always-on triggers above.

**10. CS pegged at max + SG drop.** `CS_ACTUAL` reaches maximum (≥ 30) while SG drops ≥ 30 % vs the previous step, and CS was actually regulating in the recent history (some prior step had CS < 25). The canonical slip signature with active CoolStep — load suddenly maxes out the current regulator. *Coarse phase only.*

**11. CS pegged at max + CV spike.** `CS_ACTUAL` reaches maximum while CV ≥ 5 % AND a CV spike pattern fires — intermittent slip with simultaneous current saturation. *Coarse phase only.*

**12. CS hard drop.** `CS_ACTUAL` drops by more than 8 in one step from a regulating state. Indicates the motor lost contact with the load entirely (decoupling stall — gear jumped a tooth, filament ground out, etc). *Coarse phase only.*

### Bisection-aware sensitivity

CV and IQR thresholds tighten in the bisection / verify phases — once we're already near the slip point, smaller anomalies are meaningful evidence. This keeps the coarse phase noise-resistant while making the final estimate accurate to ±1 mm³/s.

### Borderline re-test (new)

When a bisection step doesn't trigger any of the above but lands in a "gray zone" — CV between 4 % and 7 %, OR IQR between 15 and 24 raw units, AND elevated vs the coarse-phase baseline — the plugin **re-measures the same flow once** (with cool-down) before classifying it.

| Metric | Clear safe | **Gray zone** | Clear trigger |
|---|---|---|---|
| CV | < 4 % | **4 – 7 % AND ≥ 1.5× coarse-median** | ≥ 7 % (caught by trigger 3 / 6) |
| IQR | < 15 | **15 – 24 AND ≥ 1.7× coarse-median** | ≥ 25 (caught by trigger 9) |

Decision after the re-test:
- Re-test fires a real trigger → **trigger** (high = mid)
- Re-test is clearly safe (CV/IQR drop into safe range) → **safe** (low = mid)
- Re-test is borderline again → **trigger** (two consecutive borderline measurements at the same flow can't be coincidence)

Each flow is re-tested at most once per bisection run. Adds ~30–60 s in the typical case, ~5 min in the worst case (every bisection step borderline). Console messages flag re-tests so you can see when this happens.

### Other behaviour

- **Direction-independent triggers in bisection.** The bisection probes flows in arbitrary order (e.g. 120 → 115 → 118 → 116). Triggers 3–9 evaluate each step on its own merits, ignoring whether the previous probe was higher or lower.
- **Warmup exclusion**: the first run of each measurement is excluded if it deviates more than 10 % from the rest (filament pressure buildup).
- **First-trigger-wins**: the plugin saves a first-trigger snapshot, then continues with bisection/verify and updates the result if a tighter bound is found.

---

## Configuration

### 1. TMC driver

Pick the variant matching your driver and CoolStep preference. **Run the test with the same settings you use for actual printing** — don't toggle CoolStep just for the test.

> ⚠️ **Adapt before pasting:** Lines marked with `# ADAPT` depend on your specific board, wiring and motor. Don't copy these blocks blindly — at minimum, set the right pins and current for your hardware. The other lines (StallGuard / CoolStep settings) are required by the plugin and should be kept as shown.

#### TMC2240, CoolStep enabled (SG4/StealthChop path — what the plugin currently checks for)

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
driver_sg4_thrs: 80              # SG4 sensitivity (0-255, higher = more sensitive). Tune per setup.
driver_sg4_filt_en: True         # SG4 filter enabled (recommended for extruders)
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
driver_sg4_thrs: 80              # SG4 sensitivity, tune per setup
driver_sg4_filt_en: True         # SG4 filter
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
driver_SGTHRS: 100               # SG4 sensitivity (0-255, higher = more sensitive). Tune per setup.
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
driver_SGTHRS: 100               # SG4 sensitivity, tune per setup
driver_SEMIN: 0                  # required for SG-only mode
```

#### TMC5160, CoolStep enabled

```ini
[tmc5160 extruder]
cs_pin: PA15                     # ADAPT: SPI chip-select pin on your board
spi_bus: spi4                    # ADAPT: SPI bus name on your board
spi_speed: 2000000               # ADAPT only if your board needs it
run_current: 0.85                # ADAPT: motor RMS current
hold_current: 0.6                # ADAPT: typically 60–70% of run_current
sense_resistor: 0.075            # ADAPT: matches your TMC5160 module (typical 0.075 Ω)
interpolate: false               # required for clean SG readings
# IMPORTANT: TMC5160 uses StallGuard2, which only works in SpreadCycle.
# Klipper's default IS SpreadCycle (en_pwm_mode=0, tpwmthrs=0xFFFFF).
# DO NOT add a `stealthchop_threshold:` line here. In particular,
# `stealthchop_threshold: 0` does NOT disable StealthChop — it actually
# enables StealthChop with a velocity threshold of 0, which breaks
# StallGuard2.
coolstep_threshold: 0.5          # required: enables StallGuard reading above this velocity
driver_SGT: 15                   # SG2 sensitivity, signed -64..63. HIGHER = LESS sensitive
                                 # (raises SG_RESULT). Tune per setup; see "StallGuard tuning" below.
driver_SFILT: True               # SG2 filter — averages over 4 full-steps. Recommended for extruders.
# CoolStep tunables — values below are SG2-tuned (see note after next block).
# For SG4 drivers (TMC2240/2209) use SEMIN: 5, SEMAX: 2 instead.
driver_SEMIN: 2                  # SG2 typical: SG_MAX/4..SG_MAX/8 (here ≈ SG_MAX/30)
driver_SEMAX: 4                  # gives a wide CoolStep hysteresis band
driver_SEUP: 3                   # fastest current-up step
driver_SEDN: 1                   # slow current-down (good for noisy SG2)
driver_SEIMIN: 1                 # min current = 1/2 IRUN when CoolStep regulates down
```

#### TMC5160, CoolStep disabled

```ini
[tmc5160 extruder]
cs_pin: PA15                     # ADAPT
spi_bus: spi4                    # ADAPT
spi_speed: 2000000               # ADAPT only if your board needs it
run_current: 0.85                # ADAPT
hold_current: 0.6                # ADAPT
sense_resistor: 0.075            # ADAPT
interpolate: false               # required
# IMPORTANT: do NOT add `stealthchop_threshold:` — see TMC5160 CoolStep
# enabled section above for why. Klipper's default is SpreadCycle-only.
coolstep_threshold: 0.5          # required
driver_SGT: 15                   # SG2 sensitivity, signed -64..63 (higher = less sensitive)
driver_SFILT: True               # SG2 filter
driver_SEMIN: 0                  # required for SG-only mode (CoolStep disabled)
```

#### A note on driver_SEMIN for SG2 drivers (TMC5160 / TMC2130 / TMC2660)

The example above already uses `driver_SEMIN: 2`, which is appropriate for typical extruder loads on SG2 drivers. **If you copied your config from XY-stepper templates, you probably have `SEMIN: 5` instead — and that's almost always too high for an extruder.**

CoolStep ramps current **up** when `SG_RESULT < SEMIN × 32`. For `SEMIN = 5` that means SG below 160. SG2 drivers on extruders typically produce SG values in the 20–80 range, so CoolStep stays in permanent ramp-up mode and `CS_ACTUAL` sticks at 31 (max current). The plugin still detects slip correctly via the SG fallback path, and prints a diagnostic at the end of the test recommending a fitted SEMIN for your hardware.

Per Trinamic AN-002 the rule of thumb is `SEMIN ≈ SG_MAX / 4 .. SG_MAX / 8`. For typical extruder SG_MAX of 60–80, **`driver_SEMIN: 2`** (= threshold 64) is a much better starting point than 5 (= threshold 160). Adjust after your first test run based on the diagnostic output.

#### Pin reference: where to find your values

If you're not sure what pins to use:
- **Existing config**: if you already have a `[tmc2240 extruder]` or `[tmc2209 extruder]` block in your `printer.cfg`, **keep your existing pins/wiring** and just add/update the StallGuard-related lines (`stealthchop_threshold`, `coolstep_threshold`, `driver_SGT` / `driver_sg4_thrs`, `driver_SFILT` / `driver_sg4_filt_en`, `driver_SEMIN`, ...).
- **Board manual**: check your control board's documentation (BTT, Mellow, MKS, etc.) for the correct `cs_pin`/`uart_pin`/`spi_bus` values.
- **CAN toolheads**: pins prefix with `can0:` — for example `uart_pin: can0:PA15`.

### 2. StallGuard tuning

The StallGuard sensitivity (`driver_SGT`, `driver_SGTHRS`, or `driver_sg4_thrs` depending on driver) and filter (`driver_SFILT` / `driver_sg4_filt_en`) are set **directly in the `[tmcXXXX]` section** above. No `delayed_gcode` macro is needed — Klipper writes these on every startup.

#### What value to start with

| Driver | Field | Range | Direction | Starting point |
|--------|-------|-------|-----------|----------------|
| TMC2240 | `driver_sg4_thrs` | 0–255 | higher = more sensitive | 80 |
| TMC2209 | `driver_SGTHRS`   | 0–255 | higher = more sensitive | 100 |
| TMC5160 / 2130 | `driver_SGT` | −64..+63 (signed) | higher = **less** sensitive (raises SG_RESULT) | 0–6 (start), tune up if SG values are too low |
| TMC2660 | `driver_SGT` | −64..+63 (signed) | higher = less sensitive | 0 |

Filters (recommended for extruders, smooths the SG signal):

| Driver | Field | Value |
|--------|-------|-------|
| TMC2240 | `driver_sg4_filt_en` | `True` |
| TMC2209 | _(no SG filter on this chip)_ | — |
| TMC5160 / 2130 / 2660 | `driver_SFILT` | `True` |

#### How to tune

1. **Run the test once** with the starting value and watch the SG values printed live in the Klipper console (e.g. `SG median = 70`).
2. If the test triggers immediately at very low flow → **lower** sensitivity (TMC2240/2209: lower `sg4_thrs` / `SGTHRS`; TMC5160: raise `SGT`).
3. If the test reaches `MAX` without triggering → **raise** sensitivity (opposite of step 2).
4. **For TMC5160 specifically:** if the SG values look very low (median < 50 across all flows) the driver isn't producing useful range. **Raise `SGT` substantially** (e.g. from 0 to 10–20). On a Sherpa Mini extruder, `driver_SGT: 15` produced a clean SG range from ~540 (low load) down to ~90 (high load) — use that as a reference.

#### Verify with `DUMP_TMC`

After `FIRMWARE_RESTART` you can confirm the values are loaded:

```
DUMP_TMC STEPPER=extruder
```

Look for the `COOLCONF` line — it should show your `sgt`, `sfilt`, `semin`, `semax`, etc.

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
## ⚠️ Important: chopper mode required for StallGuard

This plugin needs the **right chopper mode** for your driver. This is a hardware property of the TMC chips, not the plugin:

- **TMC2209**: uses **StallGuard4 only**, which works only in **StealthChop**. SpreadCycle does not produce usable SG values during continuous extrusion. (Klipper temporarily switches modes during sensorless homing — that does not help here.)
- **TMC5160 / TMC2130 / TMC2660**: use **StallGuard2 only**, which works only in **SpreadCycle**. (This is also Klipper's default — usually you don't need to do anything.)
- **TMC2240**: special case — has **both** StallGuard4 and StallGuard2 on-chip. Works in either mode:
  - StealthChop + `sg4_thrs` non-zero → SG4 path (the plugin currently treats this as the SG4 case)
  - SpreadCycle + `sg4_thrs = 0` → SG2 path (this is the `klipper_tmc_autotune` default for the TMC2240 and the more practical configuration for printing)

### ⚠️ TMC5160 / TMC2130 / TMC2660: don't add `stealthchop_threshold`

These drivers need pure SpreadCycle. Klipper's default already gives you that — **do not add a `stealthchop_threshold:` line at all** for SG2 drivers.

> **`stealthchop_threshold: 0` is NOT the same as "no line".**
> The presence of any `stealthchop_threshold` value (including `0`) tells Klipper to enable StealthChop. With `0` it does so with a velocity threshold of 0, meaning StealthChop is active at all speeds, which breaks StallGuard2. If you previously added `stealthchop_threshold: 0` or `stealthchop_threshold: 999999` to an SG2 driver section, **remove the entire line** and `FIRMWARE_RESTART`.

### How do I know which mode I'm in?

Run in the Klipper console:

```
DUMP_TMC STEPPER=extruder
```

Look at the output:

- For **TMC2240**: line `en_pwm_mode=1` means StealthChop, `en_pwm_mode=0` means SpreadCycle
- For **TMC2209**: line `en_spreadCycle=0` means StealthChop, `en_spreadCycle=1` means SpreadCycle
- For **TMC5160 / TMC2130 / TMC2660**: should always show `en_pwm_mode=0` (SpreadCycle); `tpwmthrs=1048575` (= `0xFFFFF`) is Klipper's default and means StealthChop is permanently disabled — that's correct
- All drivers: if `tpwmthrs` is mid-range (not 0 and not 0xFFFFF), the driver switches modes based on speed — that's a problem for the test

You can also just run `TMC_FLOW_STATUS` — the plugin checks all of this and reports any issues.

### What if my SG4 extruder is on SpreadCycle?

(This applies only to TMC2209 and TMC2240 with SG4 path.) You have three options:

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

#### Option 2 — On TMC2240: just stay on SpreadCycle and use SG2

The TMC2240 has both engines. If you're already configured for SpreadCycle with `klipper_tmc_autotune` (which sets `sg4_thrs = 0`), the plugin will work via the SG2 path — no config change needed.

#### Option 3 — Switch only for the test, then back

If you really want to keep SpreadCycle for printing on a TMC2209 (which only has SG4) but still run this test:

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

#### Option 4 — Don't use this plugin

If your SG4 extruder absolutely needs SpreadCycle (very high-flow setups, specific motor characteristics), this plugin isn't the right tool for that driver. Stick to traditional flow tower prints — they don't depend on chopper mode.

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
| `COOLDOWN`         | 15      | Pause between phases (seconds)                                 |
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

# Higher flow range for fast extruders (TMC5160 setups regularly need this)
TMC_FLOW_FIND_MAX MAX=150 START=20

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

- **Prominent result panel** at the top: maximum safe flow as a large number, verification quality (CV%), and slicer recommendations (80% / 90%)
- **SG vs. flow chart** with median + IQR (P25/P75) and average
- **CS_ACTUAL vs. flow chart** (CoolStep mode only)
- **Phase markers** — vertical dashed lines at Coarse → Bisection → Verify transitions
- **Data table** with inter-run consistency (CV) for each measurement. Re-tested borderline flows appear once (the re-test result replaces the first measurement) so the table stays one-row-per-flow.
- **Stop reason** (which trigger fired and at which flow), shown below the result panel
- **TMC driver settings** captured at the start of the run — collapsible block listing all StallGuard / CoolStep-relevant fields (`SGT`, `SFILT`, `sg4_thrs`, `SEMIN`, `SEMAX`, `TCOOLTHRS`, chopper-mode flags, etc.) so you have a reproducible paper trail of the configuration that produced the result. Same block is appended as a comment header to the CSV file.

---

## About the two modes

Both modes detect filament slip — but they look at different aspects of motor behaviour. Neither is "better"; they're for different setups.

### CoolStep enabled (`driver_SEMIN > 0`)

CoolStep dynamically adjusts motor current based on load. Lower current at idle/low-flow saves energy and reduces motor heat. The driver ramps current back up when load increases.

This is what `klipper_tmc_autotune` enables by default for extruders.

The test detects slip via the [always-on triggers](#always-on-triggers-work-regardless-of-mode) (snap-back, over-jump, CV patterns, IQR spread) plus three [CoolStep-specific triggers](#coolstep-specific-triggers-only-when-coolstep-regulates-cs_actual) when CoolStep actually regulates `CS_ACTUAL`.

> **Note for SG2 drivers (TMC5160, TMC2130, TMC2660):** SG_RESULT values on these drivers tend to be much lower (typically 30–100) than on SG4 drivers, so a SEMIN value copied from XY-stepper examples (often 5) leaves CoolStep permanently in "ramp current up" mode and CS stays pinned at 31. The plugin handles this correctly — it auto-detects the static CS and uses the SG-based triggers instead. Result is identical, just via a different code path. The final report includes a CoolStep diagnostic with a recommended SEMIN tuned to your hardware. See the [CS_ACTUAL stays pinned at 31 troubleshooting entry](#cs_actual-stays-pinned-at-31-throughout-the-test).

### CoolStep disabled (`driver_SEMIN = 0`)

The motor runs at constant `IRUN` current at all times. More heat, more energy used, but maximum torque is always available — no waiting for CoolStep to ramp up.

Used by setups where motor torque reserve matters more than energy saving, or by users who prefer constant-current behaviour.

The test detects slip via the [always-on triggers](#always-on-triggers-work-regardless-of-mode) only — the CoolStep-specific triggers are skipped because there's no CoolStep regulation to observe.

### Don't switch modes just for the test

Configure your driver the way you always print, then run `TMC_FLOW_FIND_MAX`. The plugin auto-detects and picks the right mode. Switching CoolStep on/off just for the test would give you a number that doesn't apply during normal prints.

---

## Troubleshooting

### "StealthChop is not active. StallGuard needs StealthChop ON."

(SG4 drivers only — TMC2209 and TMC2240 in SG4 mode.) Your `[tmc2209 extruder]` or `[tmc2240 extruder]` section is missing `stealthchop_threshold: 999999` (or it's set to a low value that disables StealthChop at higher speeds). See [the chopper mode section](#-important-chopper-mode-required-for-stallguard) for the full explanation and options to fix it.

Quickest fix: add `stealthchop_threshold: 999999` to your extruder TMC section and `FIRMWARE_RESTART`.

### "TMC5160 / TMC2130 StallGuard2 needs SpreadCycle..."

(SG2 drivers only.) Your `[tmc5160 extruder]` (or 2130/2660) section has `stealthchop_threshold:` set, which enables StealthChop and breaks StallGuard2. **Remove the entire `stealthchop_threshold:` line** from the section — Klipper's default is already pure SpreadCycle. Then `FIRMWARE_RESTART`.

> Common pitfall: setting `stealthchop_threshold: 0` does NOT disable StealthChop. Any value (including 0) enables it. The line must be removed entirely.

### "Unable to read tmc uart 'extruder' register IFCNT"

UART communication with the TMC2209 isn't working. Not a plugin issue — Klipper can't talk to the driver at all.

- Check `uart_pin` (CAN toolheads need `can0:` prefix, e.g., `uart_pin: can0:PA15`)
- Check `uart_address` if multiple drivers share a UART
- Check sense resistor value (`sense_resistor: 0.110` for typical SilentStepSticks)
- If using `klipper_tmc_autotune`, try commenting out `[autotune_tmc extruder]` temporarily — known conflict on some setups

### "sg4_thrs is 0. StallGuard trigger inactive."

(SG4 drivers only.) The `driver_sg4_thrs` (TMC2240) or `driver_SGTHRS` (TMC2209) line is missing from your `[tmcXXXX extruder]` section. Add it and run `FIRMWARE_RESTART`:

```ini
# In [tmc2240 extruder]:
driver_sg4_thrs: 80
driver_sg4_filt_en: True

# In [tmc2209 extruder]:
driver_SGTHRS: 100
```

For a one-off runtime test (won't survive restart), you can also use:

```
SET_TMC_FIELD STEPPER=extruder FIELD=sg4_thrs VALUE=80
SET_TMC_FIELD STEPPER=extruder FIELD=sg4_filt_en VALUE=1
```

(TMC2209: `FIELD=sgthrs VALUE=100` and skip the filter line. SG2 drivers don't need this — the plugin reads SG_RESULT directly from `DRV_STATUS`.)

### "SG median = n/a" during the test

For **SG4 drivers** (TMC2240, TMC2209): the most common cause is that the extruder is in **SpreadCycle** mode instead of StealthChop. See [the chopper mode section above](#-important-chopper-mode-required-for-stallguard).

For **SG2 drivers** (TMC5160, TMC2130, TMC2660): SG_RESULT comes from `DRV_STATUS` and depends on `coolstep_threshold` (`tcoolthrs`) being set so StallGuard reads above a minimum velocity. Check that `coolstep_threshold: 0.5` (or similar low value) is set in your TMC section.

Other possible causes (any driver):
- Plugin older than the register-direct-read version — make sure you're on the latest from this repo
- TMC driver isn't responding at all — run `DUMP_TMC STEPPER=extruder` to verify communication

### CS_ACTUAL stays pinned at 31 throughout the test

This is **not** a plugin bug — it means CoolStep can't regulate because all SG values are below the lower threshold (`SEMIN × 32`). It's especially common on TMC5160 / TMC2130 / TMC2660 (SG2 drivers), where SG values are typically much lower (30–100 range) than on SG4.

The test still works correctly: when CS is static, the plugin auto-detects this and falls back to the SG-based triggers. After the test, you'll see a CoolStep diagnostic in the console with a recommended SEMIN value tuned to your hardware. Per Trinamic AN-002, `SEMIN ≈ SG_MAX/4..SG_MAX/8`.

Typical fix for SG2 drivers: lower `driver_SEMIN` from 5 (a common XY-stepper default) to 2 or 3, raise `driver_SEMAX` to 4. See the [TMC5160 CoolStep enabled config example](#tmc5160-coolstep-enabled) above.

### Reached MAX without trigger

Your extruder is faster than the default `MAX=80`. Try:

```
TMC_FLOW_FIND_MAX MAX=150
```

(TMC5160 setups regularly reach 100–130 mm³/s.)

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
- Use a smaller `COARSE_STEP` (e.g. 10 instead of 20) — bigger steps give the CV-spike trigger less context to work with

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

## Roadmap

- **TMC2240 SG2/SpreadCycle path**: the chip supports both StallGuard engines on-chip. The plugin currently expects the SG4/StealthChop combination; users running `klipper_tmc_autotune` (default `sg4_thrs = 0` → SpreadCycle/SG2) need to either configure StealthChop manually for the test, or wait for the dedicated SG2 path on the TMC2240 (in progress).
- Confirmed-tested markers for TMC2130 / TMC2660 (code path present, awaiting reports from users with that hardware).

---

## Contributing

Issues and pull requests welcome. If you've tested this on a driver not listed as fully tested above (TMC2130, TMC2226, TMC2660), let me know how it went — I'd love to add confirmed-working markers for those.
