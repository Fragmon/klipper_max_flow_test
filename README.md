# TMC Flow Test
**Adaptive max-volumetric-flow detection for 3D printer extruders using TMC StallGuard.**

Find your extruder's real max flow rate automatically — no test prints, no measuring melted noodles.

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![Klipper](https://img.shields.io/badge/Klipper-compatible-green.svg)](https://www.klipper3d.org/)
[![Kalico](https://img.shields.io/badge/Kalico-compatible-brightgreen.svg)](https://github.com/KalicoCrew/kalico)

Plugin by **Steven (Fragmon) — Crydteam**
[![YouTube](https://img.shields.io/badge/YouTube-@crydteamprinting-red?logo=youtube)](https://www.youtube.com/@crydteamprinting)

<p align="center">
  <img src="images/results.png?v=1.0" alt="TMC Flow Test HTML report" width="700">
</p>

---

## Table of contents

- [Overview](#overview)
  - [What this plugin detects](#what-this-plugin-detects)
  - [How the test works](#how-the-test-works)
  - [Supported drivers](#supported-drivers)
- [Installation](#installation)
  - [One-liner install](#one-liner-install)
  - [Verify](#verify)
  - [Updating later](#updating-later)
  - [Custom install paths](#custom-install-paths)
  - [Compatibility](#compatibility)
- [Configuration](#configuration)
  - [TMC driver section](#tmc-driver-section)
    - [TMC5160 (and TMC2130 / TMC2660)](#tmc5160-and-tmc2130--tmc2660)
    - [TMC2240](#tmc2240)
    - [TMC2209 (experimental)](#tmc2209-experimental)
  - [Plugin section](#plugin-section)
  - [Important driver settings (SFILT, CoolStep, chopper mode)](#important-driver-settings)
- [Running a test](#running-a-test)
  - [Physical setup before running](#physical-setup-before-running)
  - [Quick start command](#quick-start-command)
  - [Choosing the test range (START / MAX / COARSE_STEP)](#choosing-the-test-range)
  - [Auto-SGT calibration](#auto-sgt-calibration)
  - [Examples](#examples)
- [Understanding the results](#understanding-the-results)
  - [Output files](#output-files)
  - [HTML report structure](#html-report-structure)
  - [CSV columns](#csv-columns)
  - [How slip detection works](#how-slip-detection-works)
  - [Plateau and IQR-growth triggers](#plateau-and-iqr-growth-triggers)
  - [Thermal monitoring & cold-extrusion hints](#thermal-monitoring--cold-extrusion-hints)
- [TMC2209-specific information](#tmc2209-specific-information)
  - [Why TMC2209 is experimental](#why-tmc2209-is-experimental)
  - [Pre-flight check (mandatory)](#pre-flight-check-mandatory)
  - [TMC2209 troubleshooting](#tmc2209-troubleshooting)
- [Command reference](#command-reference)
  - [`TMC_FLOW_FIND_MAX`](#tmc_flow_find_max)
  - [`TMC_FLOW_STATUS`](#tmc_flow_status)
  - [`TMC_FLOW_TEST_SG_VARIANTS`](#tmc_flow_test_sg_variants)
- [Troubleshooting](#troubleshooting)
- [Appendix](#appendix)
  - [Per-driver tuning](#per-driver-tuning)
  - [Credits & License](#credits--license)

---

# Overview

## What this plugin detects

The primary detection target is **motor stall / extruder slip** — the point where the extruder gear loses grip on the filament. Detection is reliable across SG2 drivers (TMC5160 / TMC2240) and uses multiple independent triggers (SG patterns, run-to-run variance, IQR widening, single-step and cumulative plateaus).

**Cold-extrusion hints** (heater can't melt fast enough) are available as a heuristic visualization — the plugin computes a 0–100 thermal stress score per step combining heater PWM, temperature drop, intra-run SG drift, and other signals. This colours the chart green/yellow/red but does NOT trigger or change the slip-detection result. Always cross-check the strand visually for clean extrusion.

## How the test works

The plugin reads the TMC driver's **StallGuard** load signal during extrusion and runs four phases:

1. **Auto-SGT calibration** *(optional, on by default)* — probes SG_RESULT at the start flow and adjusts `driver_SGT` to land in the optimal sensitivity range
2. **Coarse sweep** — flow rises in big steps until StallGuard sees the limit approach
3. **Bisection** — the safe value is narrowed to ±1 mm³/s; borderline measurements are auto-re-tested
4. **Verification** — final value is confirmed with extra repeats and a stability metric; if verify fails, the test drops back into bisection with a tighter bracket

While running, the plugin also captures **thermal telemetry** (heater PWM, temperature, driver thermal flags) to give context for the result. Output: a CSV with raw data and an interactive HTML dashboard report.

A complete test typically takes **10–13 minutes**.

## Supported drivers

| Driver | Status | Mode | Detection |
|---|---|---|---|
| **TMC5160** (incl. TMC2130) | ✅ Production | SpreadCycle / SG2 | Full SG-magnitude + variance + plateau + IQR |
| **TMC2240** | ✅ Production | SpreadCycle / SG2 | Full SG-magnitude + variance + plateau + IQR |
| **TMC2209** | ⚠️ Experimental | SpreadCycle / SG4 | CV-spike (variance) only |

**Production drivers (TMC5160 / TMC2240)** — both implement **StallGuard2 (SG2)** which works in **SpreadCycle** chopper mode — the combination that delivers full motor torque AND a clean load-proportional SG signal. The plugin runs in this single, well-defined operating mode and is validated against historical data.

**Experimental driver (TMC2209)** — see [TMC2209-specific information](#tmc2209-specific-information). Skip this section unless you're specifically working with a TMC2209.

> **Important for TMC2240 users**: The plugin runs the TMC2240 in the **SG2/SpreadCycle path** (same as TMC5160). The TMC2240's SG4/StealthChop path delivers ~50 % less peak torque — it's intended for sensorless homing, not high-flow extrusion. See [TMC2240 config](#tmc2240) below.

---

# Installation

The plugin installs as a **symlink** from a cloned repo into Klipper's extras directory. This is intentional: writing a real file into `klipper/klippy/extras/` causes Moonraker / KIAUH / git to flag the Klipper repo as *dirty* or *corrupt* on its next refresh. With a symlink, the actual plugin file lives outside the Klipper repo and Klipper just follows the link.

## One-liner install

```bash
cd ~
git clone https://github.com/Fragmon/klipper_max_flow_test.git
cd klipper_max_flow_test
bash install.sh
```

That's it. The script handles everything end-to-end:

1. **Pre-flight checks** — refuses to run as root, verifies Python 3 and the Klipper service are present
2. **Validates Python syntax** of the plugin file before touching anything (catches corrupted downloads)
3. **Creates a symlink** `~/klipper/klippy/extras/tmc_flow_test.py` → `~/klipper_max_flow_test/tmc_flow_test.py`
4. **Clears stale Python caches** in `klipper/klippy/extras/__pycache__/`
5. **Auto-registers with Moonraker's update manager** — appends an `[update_manager Crydteam-Tuning-Plugins]` block to `moonraker.conf` if it isn't already there
6. **Restarts Klipper** (and Moonraker, if installed)

After the script finishes, the plugin will appear in your Mainsail / Fluidd update manager panel automatically.

> **Why `bash install.sh` and not `./install.sh`?** When uploads transit through GitHub's Web-UI or non-Git tools, the executable bit on shell scripts can get stripped. Calling the script via `bash` avoids that. The script sets its own execute bit on first run, so subsequent runs (e.g. after `git pull`) can use `./install.sh` directly.

### Re-running the install script

Re-running is safe — the script is **idempotent**:
- existing symlinks get refreshed (no duplicates)
- a real file (not symlink) gets backed up to `*.bak.<timestamp>` before being replaced
- the moonraker.conf block is added only if it isn't present already

If you previously installed manually with `wget`, just re-run `bash install.sh` — the script detects the legacy file and replaces it with a symlink cleanly.

## Verify

After install, verify in your printer console:
```
TMC_FLOW_STATUS
```

The plugin should also show up in your UI's update manager:
- **Mainsail**: Settings → Update Manager
- **Fluidd**: Settings → Updates

When new releases land on GitHub, the UI will offer an **Update** button — one click installs the latest version, restarts Klipper, done.

## Updating later

Three options, all equivalent:

- **Mainsail / Fluidd UI** — click "Update" in the update-manager panel (recommended)
- **Command line** — `cd ~/klipper_max_flow_test && git pull && sudo systemctl restart klipper`
- **Moonraker API** — `curl -X POST 'http://<your-printer>/machine/update/client?name=Crydteam-Tuning-Plugins'`

> **About the auto-restart**: The `managed_services: klipper` line in the moonraker.conf block makes Moonraker automatically restart Klipper after each update. This is safe but if you'd rather control restarts yourself, edit the block in your `moonraker.conf` to read `managed_services: ` (empty) and restart manually after each update.

## Custom install paths

If your Klipper or printer-data folder lives somewhere non-standard, set environment variables before calling the script:

```bash
KLIPPER_PATH=/path/to/klipper bash install.sh
```

The script also auto-detects whether Moonraker is installed — if `~/printer_data/config/moonraker.conf` doesn't exist, the moonraker registration step is silently skipped (Klipper-only setup is supported).

## Configuration

Configure your `[tmcXXXX extruder]` and add a `[tmc_flow_test]` section — see [Configuration](#configuration) below.

## Compatibility

| Software | Status |
|---|---|
| **Klipper** (vanilla) | ✅ Tested |
| **Kalico** | ✅ Tested (uses a local `_pstdev` to avoid Kalico's `statistics.py` shadowing stdlib) |
| **DangerKlipper** | ✅ Should work (same plugin path) |

The plugin uses only stdlib Python imports plus Klipper's `gcode`, `pins`, and `tmc` modules — no external dependencies.

---

# Configuration

## TMC driver section

Pick the variant matching your driver. **Run the test with the same chopper-mode and current you use for printing.**

> ⚠️ Lines marked `# ADAPT` depend on your hardware. The other lines are required by the plugin.

### TMC5160 (and TMC2130 / TMC2660)

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

### TMC2240

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

### TMC2209 (experimental)

> ⚠️ Read [TMC2209-specific information](#tmc2209-specific-information) first.

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

**Why no `stealthchop_threshold`?** Omitting it leaves the TMC2209 in **SpreadCycle** mode (the chip's default after Klipper init). This is the experimental setup — SG4 in SpreadCycle is unsupported per Trinamic spec but provides full motor torque and works empirically on many hardware combinations. **Always verify with `TMC_FLOW_TEST_SG_VARIANTS` first.** If your hardware fails the pre-flight check, fall back to:
```ini
stealthchop_threshold: 999999    # forces StealthChop — Trinamic-supported but ~50 % torque loss
```

**Why higher `run_current`?** TMC2209 typically runs lower currents (0.5-0.7 A) for silent operation. For max-flow testing you want full torque — set 0.85-1.0 A. Watch motor temperature; reduce `hold_current` if the motor heats up during dwell.

**Why `driver_SEMIN: 0`?** Disables CoolStep, which would otherwise modulate motor current during the test and noise up the SG signal.

> **Auto-SGT skipped for TMC2209**: The Auto-SGT calibration phase is only meaningful for SG2 drivers (TMC5160/2130/2240). On TMC2209, `driver_SGTHRS` is the DIAG-pin trigger threshold and doesn't affect SG_RESULT magnitude (per Trinamic spec). The plugin uses your configured value directly.

## Plugin section

```ini
[tmc_flow_test]
extruder_stepper: extruder       # ADAPT: stepper section name
filament_diameter: 1.75          # ADAPT: 1.75 or 2.85
melt_zone_length: 42             # ADAPT: hotend melt-zone length (see below)

# Optional:
#min_hotend_temp: 180            # safety floor, default 180
#output_dir: ~/printer_data/config/Flowtest
```

> **`melt_zone_length` matters more than you'd think.** The plugin uses this to compute residence time (how long each piece of filament spends in the heated zone) and to display reference lines for hotend classes (V6 ≥1.5 s, Volcano ≥0.6 s, CHT ≥0.3 s) on the thermal chart. Wrong values give misleading hotend-class hints.
>
> Typical values:
> - **V6 / Revo Six**: ~12–15 mm
> - **Volcano**: ~21 mm
> - **Mosquito**: ~18 mm
> - **Sherpa Mini / similar high-flow**: ~42 mm
> - **Goliath / Bondtech CHT-XL**: ~50 mm

## Important driver settings

### SFILT — keep it on

`driver_SFILT: 1` enables the StallGuard hardware filter (averages SG over 4 cycles). The plugin's slip detection thresholds are **calibrated against filtered SG signal** — running with `driver_SFILT: 0` produces noisy per-sample variance that triggers false positives in the coarse phase.

If you've previously run the test with `SFILT=0` and got early triggers (e.g. at flow=50), set `driver_SFILT: 1`, restart Klipper, and re-test.

### CoolStep — what about it?

CoolStep is the TMC feature that dynamically reduces motor current under low load. **The plugin does not require CoolStep to be on or off** — slip detection uses StallGuard signal directly.

You can keep your existing CoolStep configuration in the `[tmc<NNNN>]` section. The plugin prints a notice at test start indicating whether CoolStep is active.

For the **most conservative max-flow result**, set `driver_SEMIN: 0` (CoolStep off). With CoolStep off, the motor runs at constant `run_current` — the test then reflects what the motor can sustain at that exact current, which matches what happens during high-load printing.

### Chopper mode (important)

Each TMC chip family supports StallGuard only in one specific chopper mode:

| Driver | StallGuard variant | Required mode |
|---|---|---|
| TMC5160 / TMC2130 / TMC2660 | SG2 | **SpreadCycle** (Klipper default — **don't add `stealthchop_threshold`**) |
| TMC2240 | SG2 *(used by this plugin)* | **SpreadCycle** (don't add `stealthchop_threshold`) |
| TMC2209 | SG4 | **SpreadCycle** (experimental) or StealthChop (lower torque) |

> **`stealthchop_threshold: 0` is NOT the same as "no line".** It enables StealthChop with threshold 0, which breaks SG2. For SG2 drivers, **remove the line entirely**.

If you're not sure what mode you're in, run `TMC_FLOW_STATUS` — the plugin checks the configuration and tells you what's wrong. Or `DUMP_TMC STEPPER=extruder` and check `en_pwm_mode` (1 = StealthChop) and `tpwmthrs` (1048575 = pure SpreadCycle).

> **TMC2209 flag note:** TMC2209 inverts the StealthChop bit semantics. Its `en_spreadcycle` GCONF bit reads as 1 when SpreadCycle is active — opposite of the SG2 chips' `en_pwm_mode`. The plugin handles this internally.

---

# Running a test

## Physical setup before running

The test extrudes **a lot** of filament — at high flow rates the motor pushes around 30 mm/sec linear feed for 5 seconds at a time, with dozens of repetitions across coarse, bisection, and verification phases. Total extruded filament can easily be **2–5 meters** over the full test run.

That extruded plastic has to go somewhere. **Without preparation it piles up on the nozzle, on the bed, or wraps around the toolhead** — at minimum messy, at worst tangled around the heatblock or jammed against the print bed.

### Required: clear the area

**Before running the test, move the toolhead so molten extrusion has room to fall away cleanly.** Two good options:

**Option A — Above the bed (most printers):**
Move the toolhead to a Z-height of **at least 50–80 mm above the bed**, ideally near the centre of the print area so falling filament doesn't catch on bed clips or wires.

```
G28
G1 Z80 F600                  ; lift to 80 mm
G1 X100 Y100 F3000           ; centre over bed (adjust for your printer size)
M109 S230                    ; heat hotend (adjust temp for your filament)
```

**Option B — Off the bed entirely (recommended if printer geometry allows):**
Some printers can park the toolhead off the bed — over a purge bucket, drop chute, or just past the bed edge. This is ideal because extruded filament drops straight down without contacting anything important.

```
G28
G1 X<purge_x> Y<purge_y> Z50 F3000   ; move to purge area
M109 S230
```

### Quick checklist before you press go

- [ ] Hotend at printing temperature (check actual, not just target)
- [ ] Filament loaded and the path from spool to extruder is clear
- [ ] Spool turns freely — no tangles, snags, or "first wrap caught under the layer below"
- [ ] At least 1–2 meters of filament available on the spool (more for full discovery runs)
- [ ] Toolhead position: high above bed, or parked over an open area
- [ ] Nothing fragile near the nozzle (cables, fans, BL-Touch probes)
- [ ] If you can: lay a small piece of paper or cardboard on the bed below the nozzle to catch the dropped filament — easier cleanup

### What to expect during the test

You'll see filament extrude in chunks every few seconds. At low flow it'll come out as thin ropes; at high flow it'll come out fast and may curl or sputter. Some "spaghetti" buildup near the nozzle is normal — the plugin doesn't pause to clean between phases.

If you hear **clacking or grinding from the extruder** at high flow rates, that's the motor physically slipping. This is **expected and intentional** at the upper end of the test (it's how the plugin finds your real limit). Only stop the test (`M112`) if you hear something else — like the spool jamming, a thermistor disconnect, or the nozzle scraping the bed.

## Quick start command

For most setups (TMC5160 or TMC2240, expecting up to ~120 mm³/s):

```
TMC_FLOW_FIND_MAX MAX=120 START=25 COARSE_STEP=5
```

Test takes ~10–13 minutes total (~1–2 min for Auto-SGT calibration plus ~10 min for the actual sweep). Add 30–60 s if borderline measurements need re-testing. CSV and HTML report land in `~/printer_data/config/Flowtest/`.

> **Why `COARSE_STEP=5`?** Two of the trigger algorithms (*single-step plateau* and *IQR cumulative growth*) need at least 5 coarse steps of history to fire reliably. With `COARSE_STEP=5` you get ~20 steps for the typical range, giving every detection algorithm enough data. `COARSE_STEP=10` still works but is less sensitive on low-flow setups.

For other setups, see the table in [Choosing the test range](#choosing-the-test-range).

## Choosing the test range

The three most important parameters define the flow range and resolution of the sweep. Picking right matters: too narrow and you miss the slip point; too wide or too coarse and detection algorithms don't have enough data.

### Recommended values per hotend class

A good rule of thumb: **set MAX to ~1.5× your hotend's nominal max-flow rating, START to ~30 % of that nominal value, and use `COARSE_STEP=5` for low-flow hotends or any setup where you want maximum detection sensitivity.**

| Your hotend's nominal max-flow | `START` | `MAX` | `COARSE_STEP` | Example command |
|---|---|---|---|---|
| **~15 mm³/s** (V6 stock, Revo Six 0.4) | `5` | `25` | `5` | `TMC_FLOW_FIND_MAX START=5 MAX=25 COARSE_STEP=5` |
| **~25 mm³/s** (Volcano, CHT 0.4 on V6) | `10` | `40` | `5` | `TMC_FLOW_FIND_MAX START=10 MAX=40 COARSE_STEP=5` |
| **~30 mm³/s** (Dragon HF, Rapido HF 0.4) | `10` | `60` | `5` | `TMC_FLOW_FIND_MAX START=10 MAX=60 COARSE_STEP=5` |
| **~50 mm³/s** (Rapido HF 0.6, Mosquito Magnum) | `15` | `80` | `5` | `TMC_FLOW_FIND_MAX START=15 MAX=80 COARSE_STEP=5` |
| **~80 mm³/s** (Goliath, Bondtech CHT 0.8) | `25` | `120` | `5` | `TMC_FLOW_FIND_MAX START=25 MAX=120 COARSE_STEP=5` |
| **Unknown / want full discovery** | `10` | `150` | `5` | `TMC_FLOW_FIND_MAX START=10 MAX=150 COARSE_STEP=5` |

> **Don't know your hotend's nominal max-flow?** Check the manufacturer's spec sheet, or look up flow-test results others have published for your model. As a fallback: start with `MAX=120` — if the plugin ends without finding slip, double MAX and re-run.

> **Sanity check:** with the values you choose, you should get **at least 8 coarse measurements** before slip. If your range gives fewer, lower `COARSE_STEP`.

### What START actually does

`START` is the first flow rate the coarse sweep tests. It does NOT affect the slip-detection algorithm — only where the test begins. The plugin always steps up by `COARSE_STEP` until it sees slip indicators.

**Why not just always start at 1 mm³/s?**
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
- **COARSE_STEP too large** — with the default `COARSE_STEP=10` on a low-flow setup, you may only get 4–5 coarse measurements. The new IQR-cumulative-growth trigger needs ≥5 coarse steps to fire. Use `COARSE_STEP=5` for any setup with `MAX < 100` mm³/s.

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

> **Why probe at the start flow?** SGT effects on the SG-vs-load curve are flow-dependent. Calibrating at the exact load where the test begins gives the most representative baseline.

## Examples

```
# Recommended test for most setups (TMC5160/2240, ~50 mm³/s expected)
TMC_FLOW_FIND_MAX MAX=120 START=15 COARSE_STEP=5

# High-flow setup (Goliath, Bondtech CHT 0.8)
TMC_FLOW_FIND_MAX MAX=150 START=25 COARSE_STEP=5

# Low-flow setup (V6 stock 0.4)
TMC_FLOW_FIND_MAX MAX=30 START=5 COARSE_STEP=5

# Same test, but keep the tuned SGT after test ends
TMC_FLOW_FIND_MAX MAX=150 START=10 COARSE_STEP=5 KEEP_SGT=1

# Skip Auto-SGT and use your configured SGT directly
TMC_FLOW_FIND_MAX MAX=150 START=10 AUTO_SGT=0

# Quicker, less accurate
TMC_FLOW_FIND_MAX REPEAT=3 VERIFY_REPEATS=3 COOLDOWN=10 COARSE_STEP=10

# More accurate (longer)
TMC_FLOW_FIND_MAX REPEAT=10 DURATION=8 VERIFY_REPEATS=10 COARSE_STEP=5

# Diagnostic check
TMC_FLOW_STATUS

# TMC2209 pre-flight check (always run this first on TMC2209)
TMC_FLOW_TEST_SG_VARIANTS LOW_FLOW=30 HIGH_FLOW=140 DURATION=8

# TMC2209 main test (Auto-SGT auto-disabled, START=30 to skip SG4 bias region)
TMC_FLOW_FIND_MAX MAX=150 START=30 AUTO_SGT=0
```

---

# Understanding the results

## Output files

Files saved to `~/printer_data/config/Flowtest/`:

```
tmc_flow_YYYY-MM-DD_HH-MM-SS.csv     ← raw data, 23 columns
tmc_flow_YYYY-MM-DD_HH-MM-SS.html    ← interactive dashboard report
```

## HTML report structure

The HTML report is a dashboard layout:

- **Hero panel** — big result number (max safe flow) with 80 % / 90 % slicer recommendations and a status pill
- **Insight cards** — 4 colour-coded summaries: result quality, first trigger, thermal watch (with stress-score state), driver config
- **Tabbed charts**:
  - **StallGuard signal** — median + P25–P75 band + average, with phase markers, trigger annotations, and three-zone background colouring (green / yellow / red)
  - **Thermal profile** — heater PWM, hotend temperature, residence time per step, with V6 / Volcano / CHT reference lines
  - **Run-to-run variance** — CV bar chart per step, coloured by severity vs the trigger threshold
- **Decision timeline** — phase-by-phase summary of why the result was chosen (coarse sweep → trigger → bisect → verify)
- **Test details** (collapsible) — full data table, test configuration, TMC settings snapshot, decision trail with trigger metrics
- **Reference** (collapsible) — glossary explaining all metrics in plain language

## CSV columns

23 columns total. Slip detection columns plus thermal telemetry:

```
phase, flow_mm3s, sg_median, sg_p25, sg_p75, sg_avg, sg_min, sg_max, sg_n,
n_repeats, sg_run_cv_pct, run_sg_avgs,
temp_target, temp_start, temp_end, temp_min, temp_avg, temp_drop,
pwm_min, pwm_max, pwm_avg, tmc_otpw, tmc_ot
```

The CSV header includes the same TMC settings block as the HTML for paper-trail purposes.

## How slip detection works

The plugin samples StallGuard at 20 Hz during each measurement step (5 repeats × 5 s by default) and tracks per-step **median**, **IQR (P25–P75)**, **run-to-run CV**, and **intra-run trend** (slope of SG over time within a single run). Slip detection uses **multiple independent triggers** that look for different signatures:

- **SG signal patterns** — snap-back, over-jump, single-step plateau, 2-step cumulative plateau (with saturation-skip and median-baseline)
- **Run-to-run variance** — CV spike, CV jump, rising trend, vs coarse-baseline
- **Sample distribution** — IQR widening (single-step), IQR cumulative growth (vs early-test baseline), IQR vs coarse-baseline, IQR absolute floor
- **Per-run analysis** — single-run outlier detection (warmup-aware), SG max spike for decoupling

Each trigger fires under tighter conditions in **bisection / verify** than in coarse, so the coarse phase stays noise-resistant while the final result is accurate to ±1 mm³/s.

The HTML report's **decision-trail panel** lists every trigger event with the metrics that caused it, so you can see exactly why the plugin chose the value it did.

### Plateau and IQR-growth triggers

Two of the most important trigger types deserve explanation:

- **Single-step plateau** — fires when the most recent step's SG-delta is essentially flat compared to the recent baseline (e.g. typical −25 deltas then suddenly +0.5). Catches abrupt plateaus that a 2-step cumulative trigger would smooth over.
- **IQR cumulative growth** — fires when within-step spread has gradually doubled relative to the early-test baseline AND now exceeds an absolute floor of 12 raw units. Catches gradual stick-slip onset that single-step ratio triggers don't see.

### Warmup-skip

The first repetition of every measurement step shows different behaviour than the others (motor transitions from cold-stop, filament path settles). The plugin detects this drift and excludes run 1 from the median/IQR/CV stats when it deviates significantly from the rest. The threshold is per-driver — TMC2240 needs a more aggressive 4 % cutoff (vs. 10 % for TMC5160) because of its systematic 3–6 % first-run drift.

## Thermal monitoring & cold-extrusion hints

During every measurement the plugin captures heater PWM (avg/max), hotend temperature (target/actual/min/drop), and TMC driver thermal flags (otpw / ot). These appear in the CSV and HTML report. **They do NOT trigger anything** — they're context for interpreting the result.

A heuristic 0–100 **thermal stress score** is computed per step combining five signals:

1. **Heater PWM level** (0–30 points) — how hard the heater is working
2. **Temperature drop from target** (0–25 points) — how much actual temp falls below target
3. **PWM rising trend** (0–15 points) — heater taking on more work over the sweep
4. **PWM peak saturation hits** (0–10 points) — saturation events even briefly
5. **Intra-run SG drift** (0–20 points) — load growing within a single run = cold extrusion fingerprint

The chart backgrounds get tinted: **green (0–30 stable)** / **yellow (30–60 moderate)** / **red (60+ likely cold extrusion)**. The first flow where the score crosses 30 is marked as "cold extrusion onset" — purely visual, doesn't affect detection.

> **Why intra-run SG drift matters:** Within a single 5-second run, if SG drifts steadily toward higher load, the filament was getting harder to push as it heated. This is the physical signature of cold extrusion (vs. abrupt motor slip, which jumps suddenly without a gradient). Hover any thermal-chart point to see the per-step drift value.

---

# TMC2209-specific information

> ⚠️ This entire section only applies if you use a TMC2209 driver. Skip if you have TMC5160 or TMC2240.

## Why TMC2209 is experimental

The TMC2209 only implements **StallGuard4 (SG4)**, which Trinamic explicitly designed for StealthChop mode. From the [TMC2209 Datasheet Rev 1.09](https://www.analog.com/media/en/technical-documentation/data-sheets/tmc2209_datasheet_rev1.09.pdf):

> *"SG_RESULT becomes updated with each fullstep [...] **Intended for StealthChop mode, only.**"*

The plugin runs TMC2209 in **SpreadCycle anyway** to access full motor torque (~100 % vs. ~50 % in StealthChop). This is **outside official Trinamic specification** and works empirically on some hardware combinations but not others.

**What this means for you:**

- Results are **not** guaranteed reliable across all TMC2209 boards/motors
- Some TMC2209 boards (especially budget clones) produce unusable SG4 in SpreadCycle
- The plugin uses CV-spike detection (variance jump at slip) instead of SG-magnitude — this works when SG4 is responsive, fails silently when it isn't
- You **must** run the pre-flight check before relying on results
- Validate any max-flow result with **multiple long real-world prints** before trusting it in your slicer

If your TMC2209 doesn't pass the pre-flight check, swap to a pin-compatible TMC2240 (~€15) for guaranteed results, or use a traditional flow-tower print test.

## Pre-flight check (mandatory)

```
TMC_FLOW_TEST_SG_VARIANTS LOW_FLOW=30 HIGH_FLOW=140 DURATION=8
```

The check probes SG_RESULT in **both** StealthChop and SpreadCycle at low flow and high flow, then evaluates whether the variance signature (CV jump from low-load to slip) is detectable.

> ⚠️ **Warning**: At HIGH_FLOW values close to your hotend's limit, the motor will physically slip during the test. This is intentional — we need to see the slip signature. Be ready to stop with `M112` if you hear excessive clacking, and ensure your filament path is clear.

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

## TMC2209 troubleshooting

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

# Command reference

## `TMC_FLOW_FIND_MAX`

Run the StallGuard-based flow test (Auto-SGT → Coarse → Bisection → Verification).

| Parameter | Default | Description |
|---|---|---|
| `START` | 10 | Starting flow (mm³/s) |
| `MAX` | 80 | Upper search bound |
| `COARSE_STEP` | 10 | Coarse sweep step size (use 5 for low-flow setups) |
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

## `TMC_FLOW_STATUS`

Diagnostic check: reads current SG value, verifies driver, chopper mode, and StallGuard threshold. **Run this before the first test.**

| Parameter | Default | Description |
|---|---|---|
| `ACTIVATE` | 1 | Briefly run motor (1 mm extrusion) so SG can be read |

## `TMC_FLOW_TEST_SG_VARIANTS`

*(TMC2209 only)* Empirical pre-flight check: probes SG_RESULT in both StealthChop and SpreadCycle to determine which mode produces a usable slip signal on YOUR hardware. **Mandatory before relying on max-flow results from a TMC2209.**

| Parameter | Default | Description |
|---|---|---|
| `LOW_FLOW` | 5 | Low-load probe flow (mm³/s) |
| `HIGH_FLOW` | 20 | High-load probe flow (mm³/s) — set this near or above your hotend's expected slip point |
| `DURATION` | 5 | Seconds per probe |
| `REPEAT` | 5 | Repetitions per probe |
| `SGTHRS` | 100 | Temporary SGTHRS used for the test (your config is restored after) |

---

# Troubleshooting

**`Section 'tmc_flow_test' is not a valid config section`** —
The plugin file isn't being loaded. Common causes:
1. File at wrong path → must be `~/klipper/klippy/extras/tmc_flow_test.py`
2. Service not restarted → run `sudo systemctl restart klipper` (NOT `FIRMWARE_RESTART`)
3. Stale Python cache → `rm -f ~/klipper/klippy/extras/__pycache__/tmc_flow_test*.pyc` then restart
4. **Kalico**: see compatibility note above — the plugin needs the `_pstdev` workaround that's part of the current release

**Plugin throws `ImportError: attempted relative import with no known parent package`** —
You're on Kalico (or another Klipper fork) that ships its own `extras/statistics.py`, which shadows Python's stdlib `statistics`. The plugin works around this with a local `_pstdev` helper — make sure you have the latest version of `tmc_flow_test.py` (no `import statistics` at top of file) and re-pull from the repo if needed.

**`TMC_FLOW_STATUS` reports "StallGuard2 needs SpreadCycle"** *(SG2 drivers — TMC5160, TMC2130, TMC2240)* —
Remove the `stealthchop_threshold:` line entirely from your TMC section, restart Klipper. For TMC2240: also remove any `[delayed_gcode]` block that sets `sg4_thrs` or `sg4_filt_en` — they're not needed in SG2 mode.

**Auto-SGT can't reach target range** —
Console says "could not reach target range" after 5 iterations. Usually means your SGT is at an extreme (e.g. -64 or +63) and still doesn't produce useful SG values. Check your `run_current` — if it's very low, even max-sensitive SGT may not see enough load. Try increasing `run_current` slightly or running with `AUTO_SGT=0` and a manually-chosen SGT.

**Trigger fires very early in coarse phase (e.g. at flow=50)** —
Most common causes:
1. **SFILT is off** — check that `driver_SFILT: 1` is in your TMC section. SG noise without filter triggers false plateau detection.
2. **SGT was set too high** before Auto-SGT ran (or Auto-SGT is disabled). Re-run with default `AUTO_SGT=1`, or check the `Auto-SGT: tuned to SGT=N` line in the console output.
3. **CoolStep is masking signal** — try setting `driver_SEMIN: 0` for the most conservative result.

**Test reaches MAX without trigger** —
Either your hotend really can flow that fast (raise MAX), or SG sensitivity is still too low. Check the Auto-SGT output — if it tuned to a very high SGT (e.g. > 30), your motor torque headroom is bigger than the test's MAX value.

**Test ends without trigger and the chart shows a clear plateau** —
If `COARSE_STEP=10` skips over the slip onset, the single-step plateau trigger may not have enough resolution. Lower `COARSE_STEP` to 5 — the IQR cumulative growth trigger needs ≥5 coarse steps of history to fire, so 5 mm³/s steps give the algorithm enough data.

**TMC2240 results much lower than expected (<70 mm³/s on a fast extruder)** —
Check that you're running in **SpreadCycle/SG2** mode, not StealthChop/SG4. The SG4 path of the TMC2240 reduces peak torque by ~50 %. `TMC_FLOW_STATUS` will tell you which mode is active. Remove `stealthchop_threshold` from your `[tmc2240]` section if present.

**Result varies between runs by more than 5 mm³/s** —
Check filament consistency, hotend temperature stability, possible filament path obstructions. Increase `COOLDOWN` between phases (e.g. 30 s).

**"CoolStep is active" notice** —
The plugin works fine with CoolStep on, but for the most conservative result set `driver_SEMIN: 0` to disable CoolStep during testing. CoolStep can dampen StallGuard signal during high-load steps.

**Auto-SGT keeps tuning to the same value as my config** —
That's fine — it confirms your SGT is already optimal. The console will say "current SGT=N already optimal — no change needed".

**Thermal stress score (yellow/red zones) appears even though my heater seems fine** —
The score combines five signals; PWM peaks above 95 %, drops above 5 °C, or large intra-run SG drift each contribute. Check the chart's tooltip to see which component dominates. If only intra-run drift is high while PWM and temperature look stable, that often indicates the filament was getting harder to push during runs — possible cold extrusion or filament tangling.

For TMC2209-specific issues, see [TMC2209 troubleshooting](#tmc2209-troubleshooting).

---

# Appendix

## Per-driver tuning

Each driver family has its own `TriggerProfile` in the source code with all detection thresholds. To tune sensitivity for one driver without affecting the others, edit only its profile class.

- **TMC5160Profile** — validated production baseline (SGT=15, SFILT=1)
- **TMC2240Profile** — inherits TMC5160 base, with TMC2240-specific overrides:
  - `WARMUP_DRIFT_THRESHOLD = 0.04` — catches the systematic first-run drift
  - `PLATEAU_RATIO = 0.2` — the SG2 saturation curve is steeper on TMC2240
- **TMC2209Profile** *(experimental)* — fundamentally different detection strategy from SG2:
  - SG-magnitude triggers (snap-back, plateau, max-spike) **DISABLED** — SG4 doesn't follow the SG2 "smooth load curve" model
  - CV-spike triggers as primary slip indicator (`CV_HIGH_VARIANCE = 12.0` vs. 5.0 for SG2)
  - IQR triggers calibrated for the 0-510 SG4 scale (`IQR_ABSOLUTE_TRIGGER = 50` vs. 25 for SG2)
  - `WARMUP_DRIFT_THRESHOLD = 0.15` — SG4 has higher first-run drift

## Credits & License

Released under the **GNU GPL v3.0**. See [LICENSE](LICENSE).

Inspired by Klipper's StallGuard implementation and [klipper_tmc_autotune](https://github.com/andrewmcgr/klipper_tmc_autotune).

Plugin author: **Steven (Fragmon) — Crydteam** · [YouTube: @crydteamprinting](https://www.youtube.com/@crydteamprinting)

Contributions and feedback welcome — please open an issue or PR.
