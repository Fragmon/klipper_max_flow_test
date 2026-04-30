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

The plugin reads the TMC driver's **StallGuard** load signal during extrusion to detect when the motor is approaching slip. It runs a four-phase test:

1. **Auto-SGT calibration** *(optional, on by default)* — probes SG_RESULT at the start flow and adjusts `driver_SGT` to land in the optimal sensitivity range
2. **Coarse sweep** — flow rises in big steps until StallGuard sees the limit approach
3. **Bisection** — the safe value is narrowed to ±1 mm³/s; borderline measurements are auto-re-tested
4. **Verification** — final value is confirmed with extra repeats and a stability metric; if verify fails, the test drops back into bisection with a tighter bracket

Output: a CSV with raw data and an interactive HTML report with a **decision trail** (every trigger that fired, why, and which value defined the result).

> **Important for TMC2240 users**: The plugin runs the TMC2240 in the **SG2/SpreadCycle path** (same as TMC5160). The TMC2240's SG4/StealthChop path delivers ~50 % less peak torque — it's intended for sensorless homing, not high-flow extrusion. See [TMC2240 config](#tmc2240) below.

> **Important for TMC2209 users**: The TMC2209 has a **fundamental hardware limitation** that prevents StallGuard from working in SpreadCycle mode. This caps practical max-flow results at roughly half of what the same motor could produce on a TMC5160/2240. See [TMC2209 limitations](#tmc2209-limitations-important) below before configuring.

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
   TMC_FLOW_FIND_MAX MAX=150 START=10
   ```

Test takes ~10–13 minutes total: ~1–2 min for Auto-SGT calibration plus ~10 min for the actual sweep. Add 30–60 s if borderline measurements need re-testing. CSV and HTML report land in `~/printer_data/config/Flowtest/`.

---

## Auto-SGT calibration

The biggest factor in StallGuard accuracy is the `driver_SGT` setting. Set it too high and SG saturates at 1023 (no useful range). Set it too low and SG hits the noise floor before the motor actually slips.

The plugin's **Auto-SGT** phase (on by default) handles this for you:

1. Reads your current `driver_SGT` value
2. Probes SG_RESULT at the test's `START` flow (5 reps × 5 s, like the main test)
3. If saturation is detected (any sample at 1023) → lowers SGT
4. If SG is too low (median < 600) → raises SGT
5. Iterates until SG sits in the healthy 600–1022 range
6. Runs the actual flow test with the tuned SGT
7. **Restores your original SGT after the test** (unless `KEEP_SGT=1` is passed)

The console output recommends the tuned value for permanent inclusion in `printer.cfg`:

```
Auto-SGT: tuned to SGT=13 (was 18).
→ For a permanent fix, add to your [tmc5160 extruder] section:
    driver_SGT: 13
```

To skip Auto-SGT entirely (use your config value as-is), pass `AUTO_SGT=0`.

> **Why probe at the start flow?** SGT effects on the SG-vs-load curve are flow-dependent. Calibrating at the exact load where the test begins gives the most representative baseline. Probing at a lower flow can leave the test starting with too little dynamic range.

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
driver_SGT: 15                   # SG2 sensitivity, signed -64..63 (Auto-SGT will tune this)
driver_SFILT: 1                  # SG2 filter — REQUIRED for clean signal
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
driver_SGT: 15                   # SG2 sensitivity (Auto-SGT will tune this)
driver_SFILT: 1                  # SG2 filter — REQUIRED for clean signal
```

> **Switching from SG4 to SG2 on TMC2240?** If you previously ran the TMC2240 with `stealthchop_threshold: 999999` and a `SET_TMC_FIELD ... sg4_thrs ...` macro for sensorless homing, you'll need to disable that for the test. The plugin will detect StealthChop in `TMC_FLOW_STATUS` and tell you what to remove.

#### TMC2209

> ⚠️ **Read [TMC2209 limitations](#tmc2209-limitations-important) first.** The TMC2209 has a hardware constraint that significantly limits this plugin's effectiveness compared to TMC5160/2240. The plugin will run, but expect ~50 % lower max flow than the same motor would achieve on a TMC5160 or TMC2240.

```ini
[tmc2209 extruder]
uart_pin: PB12                   # ADAPT
run_current: 0.85                # ADAPT — see notes below
hold_current: 0.6                # ADAPT
sense_resistor: 0.110            # ADAPT
interpolate: false
stealthchop_threshold: 999999    # SG4 REQUIRES StealthChop — do not change
coolstep_threshold: 0.5          # required for StallGuard reads
driver_SGTHRS: 100               # SG4 sensitivity (0-255, higher = more sensitive)
```

**Why `stealthchop_threshold: 999999`?** TMC2209's StallGuard4 only works in StealthChop mode — there is no SG2 implementation in this chip. Setting threshold to `999999` keeps StealthChop active at all velocities (extrusion rarely exceeds this). See the limitations section below for the full hardware explanation.

**Recommended `run_current` is HIGHER than for TMC5160/2240.** StealthChop loses roughly 50 % of peak torque compared to SpreadCycle. To partially compensate, set `run_current` 20-30 % higher than you'd normally use (e.g. 0.85 A on a Sherpa Mini pancake). Keep `hold_current` modest to avoid overheating during dwell.

> **Note**: Auto-SGT is currently only supported for SG2 drivers (TMC5160, TMC2130, TMC2240). On TMC2209 the plugin uses your configured `driver_SGTHRS` value directly. Manual tuning guidance is in the [Troubleshooting](#troubleshooting) section.

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

### SFILT — keep it on

`driver_SFILT: 1` enables the StallGuard hardware filter (averages SG over 4 cycles). The plugin's slip detection thresholds are **calibrated against filtered SG signal** — running with `driver_SFILT: 0` produces noisy per-sample variance that triggers false positives in the coarse phase.

If you've previously run the test with `SFILT=0` and got early triggers (e.g. at flow=50), set `driver_SFILT: 1`, FIRMWARE_RESTART, and re-test.

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

## TMC2209 limitations (important)

The TMC2209 has a **fundamental hardware constraint** that makes it significantly less effective with this plugin than the TMC5160/2240. This isn't a plugin bug — it's a chip-level limitation that affects every Klipper-based StallGuard tool.

### What's different about the TMC2209

The TMC2209 chip family has only **one** StallGuard variant: **StallGuard4**. It does not implement StallGuard2 (the TMC5160/2240 variant). And StallGuard4 was designed by Trinamic to work **exclusively with StealthChop** — not SpreadCycle. From Trinamic's own [Application Note AN-002](https://www.analog.com/en/resources/app-notes/an-002.html):

> *"StallGuard4 differs from StallGuard2 in multiple aspects. It uses the StealthChop voltage chopper and not SpreadCycle."*

What this means in practice:

| Driver | StallGuard variant | Works in SpreadCycle? | Works in StealthChop? |
|---|---|---|---|
| TMC5160 / TMC2130 | SG2 | ✅ Yes (designed for it) | ❌ No |
| TMC2240 | SG2 + SG4 | ✅ Yes (SG2 path) | ✅ Yes (SG4 path, lower torque) |
| TMC2209 | SG4 only | ❌ Returns 0 or noise | ✅ Yes |

### The torque vs. signal trade-off

You can run a stepper motor in either chopper mode, but each mode has different torque characteristics:

- **SpreadCycle** delivers full peak torque (~100 %) — what you want for high-flow extrusion
- **StealthChop** is silent but delivers ~50 % less peak torque at high speed

For TMC5160/2240, you can have **both**: SG2 works in SpreadCycle, so you get full torque AND slip detection. For TMC2209, the choices are:

- **StealthChop + SG4** → slip detection works, but you've capped your peak torque at ~50 %
- **SpreadCycle + no SG** → full torque, but no way to detect slip (this plugin won't work)

There's no third option that gives you both. Trinamic has confirmed this in their datasheet. Multiple GitHub issues ([TMCStepper #87](https://github.com/teemuatlut/TMCStepper/issues/87), [#190](https://github.com/teemuatlut/TMCStepper/issues/190), [#293](https://github.com/teemuatlut/TMCStepper/issues/293)) document attempts to read SG_RESULT in SpreadCycle on TMC2209 — the consistent result is `0` or wildly fluctuating noise.

### What this means for your max-flow result

A given motor (say, a Sherpa Mini LDO pancake) on:

- **TMC5160 / TMC2240**: ~110-120 mm³/s (full SpreadCycle torque + reliable slip detection)
- **TMC2209**: ~50-70 mm³/s (StealthChop's reduced torque is the actual physical limit)

The plugin will run, the test will produce a valid number, and that number will reflect the genuine physical limit of your motor in StealthChop mode. It just won't reach the values your motor would achieve on a different driver.

### How to get the most out of your TMC2209

Three things help maximize what you can achieve:

1. **Increase `run_current`.** StealthChop loses torque, so compensate. Pancake steppers on Sherpa Mini are often run at 0.5-0.7 A — try 0.85-1.0 A. Watch motor temperature; reduce `hold_current` if the motor gets hot during dwell.

2. **Tune `driver_SGTHRS` carefully.** Auto-SGT doesn't currently work for SG4, so you'll need to do this manually. Start with 100. Run the test, watch the SG values in the console:
   - SG saturating at 510 (max for SG4) → SGTHRS too high, lower it (try 60-80)
   - SG hitting 0 quickly → SGTHRS too low, raise it (try 130-180)
   - SG ranging 200-400 → good

3. **Keep `interpolate: false`.** Microstep interpolation can interfere with StallGuard's load measurement. The TMC2209's native 1/16 microstepping is fine for extrusion.

### Why doesn't the plugin support SG4 in SpreadCycle?

We tested it. Empirically and from documentation: SG_RESULT in SpreadCycle on TMC2209 returns either `0` constantly or unstable values that have no correlation with actual motor load. There's no statistical signal that could be used for slip detection, and no software workaround can fix what's a chip-level limitation.

If you have a TMC2209 board and want a flow-test with full SpreadCycle torque, your options are:

- **Replace the driver with TMC2240** (pin-compatible on most boards, ~€15)
- **Use a flow-tower print** instead — this is the traditional method and works regardless of driver
- **Use [klipper_tmc_autotune](https://github.com/andrewmcgr/klipper_tmc_autotune)** to optimize your existing TMC2209 setup (it also runs in StealthChop)

We did not include a SpreadCycle test mode for TMC2209 because shipping a feature that produces unreliable results would do more harm than good — users would tune their slicer based on phantom numbers and get failed prints.

---

## How it works

The plugin samples StallGuard at 20 Hz during each measurement step (5 repeats × 5 s by default) and tracks median, IQR (P25–P75), and run-to-run CV per step. Slip detection uses **multiple independent triggers** that look for different signatures:

- **SG signal patterns** — snap-back, over-jump, plateau (with saturation-skip and median-baseline)
- **Run-to-run variance** — CV spike, CV jump, rising trend, vs coarse-baseline
- **Sample distribution** — IQR widening, IQR vs coarse-baseline, IQR absolute
- **Per-run analysis** — single-run outlier detection (warmup-aware), SG max spike for decoupling

Each trigger fires under tighter conditions in **bisection / verify** than in coarse, so the coarse phase stays noise-resistant while the final result is accurate to ±1 mm³/s.

The HTML report's **decision-trail panel** lists every trigger event with the metrics that caused it, so you can see exactly why the plugin chose the value it did.

### Warmup-skip

The first repetition of every measurement step shows different behaviour than the others (motor transitions from cold-stop, filament path settles). The plugin detects this drift and excludes run 1 from the median/IQR/CV stats when it deviates significantly from the rest. The threshold is per-driver — TMC2240 needs a more aggressive 4 % cutoff (vs. 10 % for TMC5160) because of its systematic 3–6 % first-run drift.

### Per-driver tuning

Each driver family has its own `TriggerProfile` in the source code with all detection thresholds:

- **TMC5160Profile** — validated production baseline (SGT=15, SFILT=1)
- **TMC2240Profile** — inherits TMC5160 base, with TMC2240-specific overrides:
  - `WARMUP_DRIFT_THRESHOLD = 0.04` — catches the systematic first-run drift
  - `PLATEAU_RATIO = 0.2` — the SG2 saturation curve is steeper on TMC2240
- **TMC2209Profile** — for the SG4 path

To tune sensitivity for one driver without affecting the others, edit only its profile class.

---

## Commands

### `TMC_FLOW_FIND_MAX`

Run the StallGuard-based flow test (Auto-SGT → Coarse → Bisection → Verification).

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
| `AUTO_SGT` | 1 | `1` = run Auto-SGT calibration before test (SG2 drivers only). `0` = skip |
| `KEEP_SGT` | 0 | `1` = leave the tuned SGT active until next FIRMWARE_RESTART. `0` = restore original after test |
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
# Standard test (Auto-SGT on by default)
TMC_FLOW_FIND_MAX MAX=150 START=10

# Same test, but keep the tuned SGT after test ends
TMC_FLOW_FIND_MAX MAX=150 START=10 KEEP_SGT=1

# Skip Auto-SGT and use your configured SGT directly
TMC_FLOW_FIND_MAX MAX=150 START=10 AUTO_SGT=0

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
Add `stealthchop_threshold: 999999` to your `[tmc2209 extruder]` section, `FIRMWARE_RESTART`. SG4 does not work in SpreadCycle (see [TMC2209 limitations](#tmc2209-limitations-important)).

**`TMC_FLOW_STATUS` reports "SGTHRS is 0"** *(TMC2209 only)* —
Add `driver_SGTHRS: 100` to your `[tmc2209 extruder]` section.

**TMC2209 SG_RESULT shows 0 or wildly fluctuating values** —
You're probably running in SpreadCycle (or have `stealthchop_threshold` set too low). SG4 only works in StealthChop on TMC2209 — this is a hardware limitation, not fixable in software. Set `stealthchop_threshold: 999999`. See [TMC2209 limitations](#tmc2209-limitations-important).

**TMC2209 max-flow result much lower than expected** (50-60 mm³/s on a fast extruder) —
This is expected and represents your motor's true capability in StealthChop mode. SG4 forces StealthChop, which costs ~50 % peak torque vs. SpreadCycle. Options:
1. Increase `run_current` to 0.85-1.0 A (compensates partially)
2. Switch to TMC2240 (pin-compatible on most boards) for SG2 + full SpreadCycle torque
3. Accept the result — it's the actual physical limit at this driver's torque output

**Auto-SGT can't reach target range** —
Console says "could not reach target range" after 5 iterations. Usually means your SGT is at an extreme (e.g. -64 or +63) and still doesn't produce useful SG values. Check your `run_current` — if it's very low, even max-sensitive SGT may not see enough load. Try increasing `run_current` slightly or running with `AUTO_SGT=0` and a manually-chosen SGT.

**Trigger fires very early in coarse phase (e.g. at flow=50)** —
Most common causes:
1. **SFILT is off** — check that `driver_SFILT: 1` is in your TMC section. SG noise without filter triggers false plateau detection.
2. **SGT was set too high** before Auto-SGT ran (or Auto-SGT is disabled). Re-run with default `AUTO_SGT=1`, or check the `Auto-SGT: tuned to SGT=N` line in the console output.
3. **CoolStep is masking signal** — try setting `driver_SEMIN: 0` for the most conservative result.

**Test reaches MAX without trigger** —
Either your hotend really can flow that fast (raise MAX), or SG sensitivity is still too low. Check the Auto-SGT output — if it tuned to a very high SGT (e.g. > 30), your motor torque headroom is bigger than the test's MAX value.

**TMC2240 results much lower than expected (<70 mm³/s on a fast extruder)** —
Check that you're running in **SpreadCycle/SG2** mode, not StealthChop/SG4. The SG4 path of the TMC2240 reduces peak torque by ~50 %. `TMC_FLOW_STATUS` will tell you which mode is active. Remove `stealthchop_threshold` from your `[tmc2240]` section if present.

**Result varies between runs by more than 5 mm³/s** —
Check filament consistency, hotend temperature stability, possible filament path obstructions. Increase `COOLDOWN` between phases (e.g. 30 s).

**"CoolStep is active" notice** —
The plugin works fine with CoolStep on, but for the most conservative result set `driver_SEMIN: 0` to disable CoolStep during testing. CoolStep can dampen StallGuard signal during high-load steps.

**Auto-SGT keeps tuning to the same value as my config** —
That's fine — it confirms your SGT is already optimal. The console will say "current SGT=N already optimal — no change needed".

---

## Credits & License

Released under the **GNU GPL v3.0**. See [LICENSE](LICENSE).

Inspired by Klipper's StallGuard implementation and [klipper_tmc_autotune](https://github.com/andrewmcgr/klipper_tmc_autotune).

Plugin author: **Steven (Fragmon) — Crydteam** · [YouTube: @crydteamprinting](https://www.youtube.com/@crydteamprinting)

Contributions and feedback welcome — please open an issue or PR.
