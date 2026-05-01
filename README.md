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

---

## Supported drivers

| Driver | Status | Mode | Detection |
|---|---|---|---|
| **TMC5160** (incl. TMC2130) | ✅ Production | SpreadCycle / SG2 | Full SG-magnitude + variance |
| **TMC2240** | ✅ Production | SpreadCycle / SG2 | Full SG-magnitude + variance |
| **TMC2209** | ⚠️ Experimental | SpreadCycle / SG4 | CV-spike (variance) only |

### Production drivers (TMC5160 / TMC2240)

Both implement **StallGuard2 (SG2)** which works in **SpreadCycle** chopper mode — the combination that delivers full motor torque AND a clean load-proportional SG signal. The plugin runs in this single, well-defined operating mode and is validated against historical data.

### Experimental driver (TMC2209)

> ⚠️ **EXPERIMENTAL — read this before using TMC2209**
>
> The TMC2209 only implements **StallGuard4 (SG4)**, which Trinamic explicitly designed for StealthChop mode. From the [TMC2209 Datasheet Rev 1.09](https://www.analog.com/media/en/technical-documentation/data-sheets/tmc2209_datasheet_rev1.09.pdf):
>
> > *"SG_RESULT becomes updated with each fullstep [...] **Intended for StealthChop mode, only.**"*
>
> The plugin runs TMC2209 in **SpreadCycle anyway** to access full motor torque (~100 % vs. ~50 % in StealthChop). This is **outside official Trinamic specification** and works empirically on some hardware combinations but not others.
>
> **What this means for you:**
> - Results are **not** guaranteed reliable across all TMC2209 boards/motors
> - Some TMC2209 boards (especially budget clones) produce unusable SG4 in SpreadCycle
> - The plugin uses CV-spike detection (variance jump at slip) instead of SG-magnitude — this works when SG4 is responsive, fails silently when it isn't
> - You **must** run `TMC_FLOW_TEST_SG_VARIANTS` first to verify your specific hardware works (see [TMC2209 pre-flight check](#tmc2209-pre-flight-check))
> - Validate any max-flow result with **multiple long real-world prints** before trusting it in your slicer
>
> If your TMC2209 doesn't pass the pre-flight check, swap to a pin-compatible TMC2240 (~€15) for guaranteed results, or use a traditional flow-tower print test.

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
   TMC_FLOW_FIND_MAX MAX=150 START=10
   ```

Test takes ~10–13 minutes total: ~1–2 min for Auto-SGT calibration plus ~10 min for the actual sweep. Add 30–60 s if borderline measurements need re-testing. CSV and HTML report land in `~/printer_data/config/Flowtest/`.

---

## Choosing the test range (START / MAX)

The two most important parameters for `TMC_FLOW_FIND_MAX` are `START` and `MAX` — they define the flow range the plugin sweeps. Picking these values right matters: too narrow and you'll miss the slip point; too wide and you waste time on irrelevant low-flow steps.

### Default values

```
TMC_FLOW_FIND_MAX                 # uses START=10, MAX=80
```

The defaults are tuned for **typical V6-class hotends** which run cleanly up to 10–15 mm³/s but rarely past 25–30 mm³/s. With `START=10` and `MAX=80`, the plugin sweeps from a safe baseline up to roughly 3× the maximum flow most stock hotends can sustain — which catches slip on practically any stock setup.

For high-flow hotends (Volcano, CHT, Rapido HF, Revo Voron, Mosquito Magnum) the defaults will likely terminate at MAX without finding slip. **Bump MAX upward** — see the table below.

### How to pick START and MAX

A good rule of thumb: **set MAX to ~1.5× your hotend's nominal max-flow rating, and START to ~30 % of that nominal value.** That gives the plugin enough headroom above the rated limit to find real slip, and a reasonable starting point that's already in the SG-informative region.

| Your hotend's nominal max-flow | Suggested `START` | Suggested `MAX` | Example command |
|---|---|---|---|
| **~15 mm³/s** (V6 stock, Revo Six 0.4) | `5` | `25` | `TMC_FLOW_FIND_MAX START=5 MAX=25` |
| **~25 mm³/s** (Volcano, CHT 0.4 on V6) | `10` | `40` | `TMC_FLOW_FIND_MAX START=10 MAX=40` |
| **~30 mm³/s** (Dragon HF, Rapido HF 0.4) | `10` | `50` | `TMC_FLOW_FIND_MAX START=10 MAX=50` |
| **~50 mm³/s** (Rapido HF 0.6, Mosquito Magnum) | `20` | `80` | `TMC_FLOW_FIND_MAX START=20 MAX=80` |
| **~80 mm³/s** (Goliath, Bondtech CHT 0.8) | `30` | `120` | `TMC_FLOW_FIND_MAX START=30 MAX=120` |
| **Unknown / want full discovery** | `10` | `150` | `TMC_FLOW_FIND_MAX START=10 MAX=150` |

> **Don't know your hotend's nominal max-flow?** Check the manufacturer's spec sheet, or look up flow-test results others have published for your model. As a fallback: start with `MAX=80` (the default) — if the plugin ends without finding slip, double MAX and re-run.

### What START actually does

`START` is the first flow rate the coarse sweep tests. It does NOT affect the slip-detection algorithm — only where the test begins. The plugin always steps up by `COARSE_STEP` (default 10 mm³/s) until it sees slip indicators.

**Why not just always start at 1 mm³/s?** Two reasons:
- StallGuard signal at very low flow rates can be noisy (especially on TMC2209 where there's a "low-velocity bias region" below ~30 mm³/s)
- Each step takes ~25 seconds (5 reps × 5 s). Starting too low wastes minutes on flows that obviously won't slip.

A good START is **above the SG noise floor** but **well below your expected slip point**. The defaults handle this for typical setups; the table above adjusts for your specific hotend.

### What MAX actually does

`MAX` is the upper safety cap. The plugin stops the coarse sweep at MAX even if no slip was detected. If you reach MAX without trigger, you'll see:

```
>>> Reached MAX (80.0 mm³/s) without trigger.
```

This means either:
1. Your hotend can genuinely flow more than MAX (rare on stock setups, common with CHT/Volcano/Rapido)
2. Your StallGuard sensitivity is too low to detect slip
3. Your `run_current` is so high the motor never actually slips at the tested flow rates

If you hit MAX without trigger, **double MAX** and re-run. If you hit MAX a second time, check `TMC_FLOW_STATUS` for sensitivity issues.

### Common pitfalls

- **Setting START too high** — plugin starts at e.g. 50 mm³/s on a hotend that slips at 40. Result: trigger fires immediately at the first step and bisection narrows downward. Better: start lower so coarse-baseline statistics are well-established before slip.
- **Setting MAX equal to expected flow** — leaves no headroom for the coarse sweep to overshoot. Always set MAX at least 30–50 % above your expected slip point.
- **Setting MAX = 999** — wastes time on flows your hardware can't physically sustain. Pick a realistic upper bound based on the hotend.
- **Using the default for a known high-flow setup** — stock 80 mm³/s default is too low for CHT-equipped Volcano hotends. Bump to 120–150.

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

#### TMC2209 (experimental)

> ⚠️ Read the [Supported drivers](#experimental-driver-tmc2209) and [TMC2209 pre-flight check](#tmc2209-pre-flight-check) sections first.

```ini
[tmc2209 extruder]
uart_pin: PB12                   # ADAPT
run_current: 0.85                # ADAPT — see notes below
hold_current: 0.5                # keep modest to avoid overheating
sense_resistor: 0.110            # ADAPT (typical 0.110 on most boards)
interpolate: false
# stealthchop_threshold INTENTIONALLY OMITTED — see notes below
coolstep_threshold: 0.5          # required for StallGuard reads
driver_SGTHRS: 100               # SG4 threshold
driver_SEMIN: 0                  # CoolStep off — clean SG signal
```

**Why no `stealthchop_threshold`?** Omitting this Klipper config option leaves the TMC2209 in **SpreadCycle** mode (the chip's default after Klipper init). This is the experimental setup — SG4 in SpreadCycle is unsupported per Trinamic spec but provides full motor torque and works empirically on many hardware combinations. **Always verify with `TMC_FLOW_TEST_SG_VARIANTS` first.**

If your hardware fails the pre-flight check, you can fall back to the documented (but lower-torque) StealthChop mode by adding:
```ini
stealthchop_threshold: 999999    # forces StealthChop — Trinamic-supported but ~50 % torque loss
```

**Why higher `run_current`?** The TMC2209 typically runs lower currents (0.5-0.7 A) for silent operation. For max-flow testing you want full torque — set 0.85-1.0 A. Watch motor temperature; reduce `hold_current` if the motor heats up during dwell.

**Why `driver_SEMIN: 0`?** Disables CoolStep, which would otherwise modulate motor current during the test and noise up the SG signal.

> **Auto-SGT skipped for TMC2209**: The Auto-SGT calibration phase is only meaningful for SG2 drivers (TMC5160/2130/2240). On TMC2209, `driver_SGTHRS` is the DIAG-pin trigger threshold and doesn't affect SG_RESULT magnitude (per Trinamic spec). The plugin uses your configured value directly.

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

> **`stealthchop_threshold: 0` is NOT the same as "no line".** It enables StealthChop with threshold 0, which breaks SG2. For SG2 drivers, **remove the line entirely**.

If you're not sure what mode you're in, run `TMC_FLOW_STATUS` — the plugin checks the configuration and tells you what's wrong. Or `DUMP_TMC STEPPER=extruder` and check `en_pwm_mode` (1 = StealthChop) and `tpwmthrs` (1048575 = pure SpreadCycle).

---

## TMC2209 pre-flight check

> ⚠️ **Mandatory step before TMC2209 max-flow tests.** TMC2209 SG4 in SpreadCycle is unsupported per Trinamic spec. Whether it works on YOUR hardware is empirical — this command tells you.

```
TMC_FLOW_TEST_SG_VARIANTS LOW_FLOW=30 HIGH_FLOW=140 DURATION=8
```

The check probes SG_RESULT in **both** StealthChop and SpreadCycle at low flow and high flow, then evaluates whether the variance signature (CV jump from low-load to slip) is detectable.

> **Warning**: At HIGH_FLOW values close to your hotend's limit, the motor will physically slip during the test. This is intentional — we need to see the slip signature. Be ready to stop with `M112` if you hear excessive clacking, and ensure your filament path is clear.

### What "USABLE" means

The plugin reports each mode as USABLE or NOT USABLE based on:

- **CV-spike detected** (CV ratio ≥ 3× between low and high flow, with high CV ≥ 10 %) → real slip will trigger detection
- **Large SG-magnitude change** (|delta| ≥ 50) → magnitude-based triggers can also work as a backup

### Reading the recommendation

The output ends with a **Recommendation** block telling you which chopper mode to use. Three typical outcomes:

**Both modes USABLE, SpreadCycle stronger** — best case. SpreadCycle gives full torque AND a clean slip signal. Omit `stealthchop_threshold` from your config.

**Only StealthChop USABLE** — your hardware doesn't produce reliable SG4 in SpreadCycle. Add `stealthchop_threshold: 999999` to your config and accept ~50 % torque loss.

**Neither mode USABLE** — your TMC2209 hardware can't produce a usable SG4 signal for this purpose. Try increasing `run_current`, increase HIGH_FLOW so the motor actually reaches stall, or accept that this hardware combo isn't suitable. As a last resort, swap to TMC2240 or use a flow-tower print.

### Caveats even when "USABLE"

- The TMC2209 SG signal is much noisier than SG2. The plugin uses tighter CV-based triggers than for TMC5160/2240 to compensate, but borderline measurements may need re-testing.
- SG_RESULT magnitude on TMC2209 often goes the "wrong direction" (rises with load instead of falling). The plugin's CV-spike triggers are direction-agnostic and work regardless.
- Always validate the final max-flow value with **multiple long real-world prints** before configuring it as your slicer's max volumetric speed.

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
- **TMC2209Profile** *(experimental)* — fundamentally different detection strategy from SG2:
  - SG-magnitude triggers (snap-back, plateau, max-spike) **DISABLED** — SG4 doesn't follow the SG2 "smooth load curve" model
  - CV-spike triggers as primary slip indicator (`CV_HIGH_VARIANCE = 12.0` vs. 5.0 for SG2)
  - IQR triggers calibrated for the 0-510 SG4 scale (`IQR_ABSOLUTE_TRIGGER = 50` vs. 25 for SG2)
  - `WARMUP_DRIFT_THRESHOLD = 0.15` — SG4 has higher first-run drift

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

### `TMC_FLOW_TEST_SG_VARIANTS` *(TMC2209 only)*

Empirical pre-flight check: probes SG_RESULT in both StealthChop and SpreadCycle to determine which mode produces a usable slip signal on YOUR hardware. **Mandatory before relying on max-flow results from a TMC2209.**

| Parameter | Default | Description |
|---|---|---|
| `LOW_FLOW` | 5 | Low-load probe flow (mm³/s) |
| `HIGH_FLOW` | 20 | High-load probe flow (mm³/s) — set this near or above your hotend's expected slip point |
| `DURATION` | 5 | Seconds per probe |
| `REPEAT` | 5 | Repetitions per probe |
| `SGTHRS` | 100 | Temporary SGTHRS used for the test (your config is restored after) |

---

## Examples

```
# Standard test (TMC5160/2240 with Auto-SGT on by default)
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

# TMC2209 pre-flight check (always run this first on TMC2209)
TMC_FLOW_TEST_SG_VARIANTS LOW_FLOW=30 HIGH_FLOW=140 DURATION=8

# TMC2209 main test (Auto-SGT auto-disabled, START=30 to skip SG4 bias region)
TMC_FLOW_FIND_MAX MAX=150 START=30 AUTO_SGT=0
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

### TMC2209-specific (experimental)

**`TMC_FLOW_TEST_SG_VARIANTS` reports both modes "NOT USABLE"** —
Possible causes:
1. HIGH_FLOW too low — motor never reaches actual stall. Increase HIGH_FLOW until you hear physical clacking during the test.
2. `run_current` too low — motor has too much torque headroom to slip. Try 0.85-1.0 A.
3. SG_RESULT stays at 0 or one fixed value across both modes → hardware-level SG4 problem (some TMC2209 clones have non-functional SG4). Try a different TMC2209 board or switch to TMC2240.

**TMC2209 SG values seem to "go the wrong way"** (rise with load instead of fall) —
That's normal on TMC2209 SG4. The Trinamic spec says higher SG = lower load, but in practice on many TMC2209 boards SG_RESULT magnitude is unreliable. The plugin's CV-spike triggers are direction-agnostic and detect slip regardless. As long as the pre-flight check reports "USABLE", the main test will work.

**TMC2209 main test ends very early or very late** —
The CV-based triggers tuned in `TMC2209Profile` are necessarily looser than for SG2 to handle SG4 noise. If borderline measurements seem off, try:
- Higher `REPEAT` (e.g. 10) for tighter run-to-run statistics
- Higher `VERIFY_REPEATS` (e.g. 10) for more confident verification
- Verify with multiple long real-world prints — TMC2209 results are inherently less reliable than SG2

**TMC2209 main test passes but real prints under-extrude at the recommended flow** —
Expected risk with the experimental TMC2209 path. The Trinamic-supported way is StealthChop only (and ~50 % torque loss). Either:
1. Drop the slicer max-flow value 20 % below what the plugin reported
2. Re-test with `stealthchop_threshold: 999999` for the conservative documented mode
3. Switch to TMC2240 for a guaranteed-reliable result

---

## Credits & License

Released under the **GNU GPL v3.0**. See [LICENSE](LICENSE).

Inspired by Klipper's StallGuard implementation and [klipper_tmc_autotune](https://github.com/andrewmcgr/klipper_tmc_autotune).

Plugin author: **Steven (Fragmon) — Crydteam** · [YouTube: @crydteamprinting](https://www.youtube.com/@crydteamprinting)

Contributions and feedback welcome — please open an issue or PR.
