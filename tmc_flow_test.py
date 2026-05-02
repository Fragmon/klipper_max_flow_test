# TMC Flow Test — Adaptive max-volumetric-flow detection for extruders
# credits:
#   Steven (Fragmon) — Crydteam
#   YouTube: https://www.youtube.com/@crydteamprinting
#
# License: GPLv3

import logging
import math
import os
import time
import json


def _pstdev(values):
    """Population standard deviation. Local implementation to avoid
    importing Python's stdlib `statistics` module — Kalico ships an
    extras/statistics.py plugin that shadows it when this file is
    imported from klipper/klippy/extras."""
    n = len(values)
    if n == 0:
        return 0.0
    mean = sum(values) / n
    variance = sum((x - mean) ** 2 for x in values) / n
    return math.sqrt(variance)


SAMPLE_INTERVAL = 0.05    # 20 Hz polling
MIN_HOTEND_TEMP = 180.0
MODULE_NAME = "TMC Flow Test"
MODULE_VERSION = "3.0"
SG_MIN_INFORMATIVE = 50   # below this SG value, readings are noise


# ─── Driver-specific trigger profiles ──────────────────────────────────
# Each TMC chip family has its own SG noise characteristics, scale, and
# CoolStep behaviour. Rather than embedding "if self.sg2_driver / else"
# branches throughout the trigger code, all tunable thresholds are
# centralized here in a profile per driver. To adjust sensitivity for
# one driver family without affecting the others, edit only its profile.
#
# The base profile contains the values that have been validated against
# real TMC5160 test runs (CSVs from 11-37, 12-40, 12-56, 13-20). Other
# drivers START as identical copies and can be tuned independently from
# real data.

class TriggerProfile:
    """Threshold values used by the slip-detection triggers.

    Inherit and override only the fields that need tuning for a
    specific driver family. Values here are the validated TMC5160
    defaults — DO NOT change them without re-running the TMC5160
    regression suite.
    """

    # ─── _check_cv_spike thresholds ────────────────────────────────
    # Pattern (a) "high-variance trip" — last CV above this absolute
    # value triggers immediately, regardless of baseline.
    CV_HIGH_VARIANCE = 10.0

    # Pattern (b) "ratio jump from immediate baseline"
    CV_JUMP_RATIO_COARSE = 2.5
    CV_JUMP_RATIO_BISECT = 2.0
    CV_JUMP_MIN_COARSE = 5.0
    CV_JUMP_MIN_BISECT = 4.0

    # Pattern (c) "rising trend" — three consecutive ≥1.3× steps
    CV_RISING_RATIO = 1.3
    CV_RISING_MIN_PRIOR = 2.5
    CV_RISING_MIN_LAST_COARSE = 5.0
    CV_RISING_MIN_LAST_BISECT = 4.0

    # Pattern (d) "vs coarse-phase baseline" (bisection only)
    CV_VS_COARSE_RATIO = 2.0
    CV_VS_COARSE_MIN = 5.0

    # Single-step low-baseline jump check
    CV_LOWBASE_RATIO = 1.5

    # ─── _check_iqr_spread thresholds ──────────────────────────────
    # Pattern (a) "ratio vs immediate prior steps"
    IQR_RATIO_COARSE = 3.0
    IQR_RATIO_BISECT = 1.7
    IQR_RATIO_MIN_ABS = 12   # don't fire on tiny absolute IQRs
    # Pattern (a) CV cross-check: when True, pattern (a) only fires
    # if CV ALSO confirms elevated noise relative to prior steps.
    # Helps drivers (e.g. SG4 with active CoolStep) where CS regulation
    # widens within-step IQR without affecting run-to-run reproducibility
    # (CV stays low). TMC5160 was validated with this OFF — leave it
    # off for SG2 unless real data shows false positives there too.
    IQR_RATIO_REQUIRE_CV = False
    IQR_RATIO_CV_FLOOR_COARSE = 3.0   # absolute CV floor, coarse phase
    IQR_RATIO_CV_FLOOR_BISECT = 2.5   # absolute CV floor, bisection

    # Pattern (b) "vs coarse-phase median"
    IQR_VS_COARSE_RATIO = 2.5
    IQR_VS_COARSE_MIN_ABS = 18
    IQR_VS_COARSE_REQUIRE_CV = True   # CV cross-check on by default
    IQR_VS_COARSE_CV_FLOOR = 3.0
    IQR_VS_COARSE_CV_RATIO = 1.5

    # Pattern (c) "absolute" — trigger if IQR ≥ this in bisection
    IQR_ABSOLUTE_TRIGGER = 25

    # ─── _check_sg_max_spike thresholds ────────────────────────────
    SG_MAX_RATIO_TO_MEDIAN = 3.0
    SG_MAX_RATIO_TO_COARSE = 1.3
    SG_MAX_ABS_GAP = 200            # absolute gap floor (sg2_driver)
    SG_MAX_ABS_GAP_SG4 = 150        # absolute gap floor (sg4 chips)
    SG_MAX_BIG_RATIO = 4.0          # alt path — extreme ratio
    SG_MAX_BIG_GAP = 300            # alt path — extreme gap
    # Inverse direction (rare case where SG rises with load)
    SG_MIN_RATIO_TO_MEDIAN = 3.0
    SG_MIN_ABS_GAP = 80

    # ─── _check_run_outlier thresholds ─────────────────────────────
    OUTLIER_MAD_RATIO = 4.0         # deviation ≥ this × MAD
    OUTLIER_MIN_REL = 0.08          # AND ≥ 8 % of median

    # ─── _is_borderline thresholds ─────────────────────────────────
    BORDER_CV_LOW = 4.0
    BORDER_CV_HIGH = 7.0
    BORDER_CV_RATIO = 1.5
    BORDER_IQR_LOW = 15
    BORDER_IQR_HIGH = 25
    BORDER_IQR_RATIO = 1.7
    BORDER_IQR_CV_FLOOR = 2.0       # CV must be at least this for IQR-only borderline

    # ─── SG-level / step-jump basics ───────────────────────────────
    # Used by _sg_min_informative and _sg_jump_threshold.
    # Note: sg2_driver returns -1 (no gating) for SG_MIN_INFORMATIVE,
    # while SG4 returns the SG_MIN_INFORMATIVE module constant.
    SG_JUMP_THRESHOLD = 5           # SG2: small jumps significant

    # ─── Plateau trigger threshold ─────────────────────────────────
    # The plateau trigger fires when the cumulative SG-load over 2
    # steps falls short of expected_2step × this fraction. Lower
    # values (e.g. 0.3) mean the trigger is more permissive — only
    # really flat sections fire. Higher values (e.g. 0.6) mean any
    # mild deceleration of the SG decline fires.
    #
    # TMC5160 baseline of 0.5 was tuned with real Sherpa data. TMC2240
    # SG2 has a steeper SG-vs-load saturation curve (each delta is
    # half the previous), so 0.5 fires very early on the natural
    # saturation slope. Lower this fraction for TMC2240.
    PLATEAU_RATIO = 0.5             # cumulative-load < this × expected_2step

    # ─── Plateau saturation-skip threshold ─────────────────────────
    # The plateau trigger compares the current step's SG-delta to the
    # average of the prior 3 deltas. If any prior step had SG values
    # close to the 1023 saturation ceiling, those step's delta is
    # artificially inflated (clipping artefact) — and the plateau
    # trigger then thinks "load should have dropped MUCH more than
    # it did" and falsely fires on naturally saturating curves.
    #
    # Skip plateau evaluation entirely if any prior SG median exceeds
    # this threshold. The validated TMC5160 11-37-30 baseline has
    # max prior SG ~536 (well below 700). A test starting at flow=10
    # with SGT≥15 typically sees SG=850-1023 on the first step —
    # that's the case we need to skip.
    PLATEAU_SATURATION_SKIP = 700   # skip plateau if prior SG > this

    # ─── Warmup-skip threshold ─────────────────────────────────────
    # Per-step, the first repetition often shows different SG behaviour
    # than the others (motor transitions from cold-stop to extruding,
    # filament path settles, etc.). If run 1's SG average deviates from
    # the rest by MORE than this fraction, it's flagged as warmup and
    # excluded from the median/IQR/CV stats. The TMC5160 baseline
    # (10 %) was tuned with real Sherpa Mini data; TMC2240 needs a
    # more aggressive threshold because its run-to-run drift is
    # systematically higher (typically 3-6 %) — without dropping
    # warmup, the run-outlier trigger fires repeatedly on these
    # warmup runs and aborts the test far below real slip.
    WARMUP_DRIFT_THRESHOLD = 0.10   # 10 % default (TMC5160 baseline)

    # ─── Auto-SGT-tuning ranges (SG2 drivers) ──────────────────────
    # Used by the optional pre-test SGT auto-tuning phase. Values
    # apply to drivers using the signed `sgt` field (TMC5160, TMC2130,
    # TMC2240 in SG2 mode). TMC2209 (SG4) uses different field/range
    # and is not auto-tuned in this version.
    #
    # Probe strategy: extrude AT the user's START flow (the same flow
    # the test will begin coarse sweeping from). Calibrating SGT for
    # exactly this load gives the most representative SG-vs-load
    # baseline — which matters because SGT effects are flow-dependent
    # and a probe-derived SGT only matches the test if the probe flow
    # matches the start flow.
    #
    # Targets are evaluated at the start-flow load:
    #   - SGT_LOW_TARGET_MIN: SG should be at least this for adequate
    #                         dynamic range. Below → bump SGT up.
    #   - SGT_LOW_TARGET_MAX: SG should NOT exceed this. Slightly
    #                         below the 1023 saturation ceiling so a
    #                         truly-saturated reading is detected.
    SGT_LOW_TARGET_MIN = 600        # SG at start_flow ≥ this
    SGT_LOW_TARGET_MAX = 1022       # SG at start_flow ≤ this (1023=saturated)
    SGT_RANGE_MIN = -64             # absolute SGT lower bound (SG2)
    SGT_RANGE_MAX = 63              # absolute SGT upper bound (SG2)
    SGT_AUTOTUNE_PROBE_DURATION = 5.0  # seconds per probe (matches test)
    SGT_AUTOTUNE_PROBE_REPEATS = 5     # repetitions per probe (statistical)
    SGT_AUTOTUNE_MAX_ITERATIONS = 5    # safety cap


class TMC5160Profile(TriggerProfile):
    """TMC5160 / TMC2130 / TMC2660 — SG2 + SpreadCycle.

    These are the validated production values. Do not change without
    running the regression suite against the historical CSVs.
    """
    pass  # uses base defaults (= validated 5160 values)


class TMC2240Profile(TriggerProfile):
    """TMC2240 — SG2 + SpreadCycle (high-torque path).

    The plugin runs TMC2240 in the same SG2/SpreadCycle path as the
    TMC5160, because the SG4/StealthChop path delivers ~50 % less peak
    torque (confirmed empirically: same Sherpa Mini reaches 110 mm³/s
    on TMC5160 SG2 but only 60 mm³/s on TMC2240 SG4).

    The TMC2240 SG2 hardware path produces nearly identical SG signal
    characteristics to the TMC5160 — but with one systematic
    difference: the first repetition of every measurement step shows
    a 3-6 % SG drift compared to runs 2-5. This warmup pattern is
    consistent across all flow steps (confirmed with CSV 21-02-45).
    The default TMC5160 warmup-skip threshold (10 %) doesn't catch
    these drifts in the coarse phase but does catch them in
    bisection / verify, where the run-outlier trigger then
    erroneously interprets the warmup run as slip — producing a
    max-flow result that's roughly half the real hardware capability.

    Fix: lower the warmup-skip threshold so run 1 is reliably
    excluded throughout the test. All other thresholds inherit from
    the validated TMC5160 baseline.

    Second fix: lower the plateau-trigger threshold. The TMC2240 SG2
    saturation curve is steeper than the TMC5160 — observed ratios
    of 0.23 between consecutive deltas during natural saturation. The
    TMC5160 default of 0.5 fires far too early on this curve. Setting
    PLATEAU_RATIO to 0.2 makes the trigger fire only when the trend
    truly flattens (which is when slip actually begins).
    """
    WARMUP_DRIFT_THRESHOLD = 0.04   # 4 % — catches the systematic drift
    PLATEAU_RATIO = 0.2             # steeper saturation than TMC5160


class TMC2209Profile(TriggerProfile):
    """TMC2209 — SG4 in StealthChop or SpreadCycle.

    Empirical findings on TMC2209 (Sherpa Mini, validated 2026-04-30):

    Unlike the SG2 drivers (TMC5160 / TMC2240), TMC2209 SG4_RESULT
    behaves very differently and the SG-median trigger logic that
    works on SG2 does NOT apply:

    1. SG_RESULT scale is 0-510 (not 0-1023). Trinamic spec says
       "higher = lower load" same as SG2, but in practice SG_RESULT
       on TMC2209 only produces meaningful values above a hardware-
       specific minimum velocity — below that it sticks in a "bias
       region" of 6-22 regardless of actual load.

    2. SG-median MAGNITUDE is unreliable as a slip indicator. On
       validated TMC2209 hardware, SG actually INCREASED with load
       (from 6 at low flow to 86 at near-stall), the opposite of
       what Trinamic's spec describes. This is why Klipper's
       sensorless homing on TMC2209 only uses the binary DIAG-pin
       trigger (SG < 2*SGTHRS), not the SG_RESULT magnitude itself.

    3. SG run-to-run CV (variance) IS a reliable slip indicator.
       At validated stall point (flow=140, motor physically slipped):
         CV jumped from 2.4% (no slip) to 23.3% (full stall).
       That's a 10x increase — easily detectable.

    Strategy: trigger primarily on CV-spike and IQR-widening. Disable
    SG-median-based triggers (snap-back, plateau, max-spike) since
    they assume the SG2 "smooth load curve" model that doesn't apply.
    """
    # ─── SG-median triggers DISABLED ──────────────────────────────
    # Set thresholds extremely high so they never fire. SG2 logic
    # (snap-back / plateau / max-spike) doesn't apply to SG4.
    SG_JUMP_THRESHOLD = 99999       # disable sudden-jump trigger
    SG_MAX_SPIKE_RATIO = 99.0       # disable max-spike (decoupling) trigger
    SG_MAX_SPIKE_RATIO_BISECT = 99.0
    PLATEAU_RATIO = 0.0             # 0 → never fire plateau
    PLATEAU_SATURATION_SKIP = 0     # n/a since plateau disabled

    # ─── CV-based triggers TIGHTENED ──────────────────────────────
    # Empirically: CV ~2-7 % during clean operation, jumps to 20-30 %
    # at slip. Set CV_HIGH_VARIANCE high enough to ignore noise but
    # low enough to catch the slip jump.
    CV_HIGH_VARIANCE = 12.0         # absolute CV trigger (vs 5.0 on SG2)
    CV_JUMP_RATIO_BISECT = 3.0      # 3x baseline jump in bisection
    CV_JUMP_RATIO_COARSE = 4.0      # 4x for coarse (noise-tolerant)
    CV_RISING_BISECT = 8.0          # rising CV trend trigger
    CV_RISING_COARSE = 12.0
    CV_VS_COARSE_BISECT = 4.0       # 4x coarse-baseline CV
    CV_VS_COARSE_COARSE = 6.0
    CV_LOWBASE_RATIO = 5.0          # only trigger if base CV > 1 %
    CV_FLOOR = 1.0

    # ─── IQR-based triggers TIGHTENED ─────────────────────────────
    # IQR also widens dramatically at slip. On the 0-510 scale the
    # absolute IQR threshold needs to be lower than for SG2.
    IQR_RATIO_COARSE = 5.0
    IQR_RATIO_BISECT = 4.0
    IQR_VS_COARSE_BISECT = 6.0
    IQR_ABSOLUTE_TRIGGER = 50       # raw SG units (vs 25 on SG2)
    REQUIRE_CV_CROSS_CHECK = True   # IQR alone needs CV confirmation

    # ─── Border / sanity ──────────────────────────────────────────
    BORDER_CV_LOW = 6.0             # CV between 6-12% = borderline
    BORDER_CV_HIGH = 12.0
    BORDER_IQR_LOW = 30
    BORDER_IQR_HIGH = 50

    # ─── Outlier detection (single-run slip in 5 reps) ────────────
    OUTLIER_MAD_RATIO = 4.0
    OUTLIER_MIN_REL = 0.15          # relaxed for SG4 noise (vs 0.08 on SG2)

    # ─── Warmup-skip ──────────────────────────────────────────────
    WARMUP_DRIFT_THRESHOLD = 0.15   # SG4 has higher first-run drift


def get_trigger_profile(driver_type):
    """Return the appropriate TriggerProfile subclass for a given driver."""
    if driver_type == 'tmc2240':
        return TMC2240Profile
    if driver_type == 'tmc2209':
        return TMC2209Profile
    # Default: TMC5160-style SG2 profile (covers tmc5160, tmc2130, tmc2660,
    # and any unknown driver — safest fallback).
    return TMC5160Profile


class TMCFlowTest:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')

        self.stepper_name = config.get('extruder_stepper', 'extruder')
        self.filament_diameter = config.getfloat(
            'filament_diameter', 1.75, above=0.)
        self.melt_zone_length = config.getfloat(
            'melt_zone_length', 42.0, above=0.)
        self.min_hotend_temp = config.getfloat(
            'min_hotend_temp', MIN_HOTEND_TEMP, above=0.)

        config_dir = os.path.expanduser('~/printer_data/config')
        if not os.path.isdir(config_dir):
            config_dir = os.path.expanduser('~')
        default_dir = os.path.join(config_dir, 'Flowtest')
        self.output_dir = config.get('output_dir', default_dir)

        self.filament_area = math.pi * (self.filament_diameter / 2.0) ** 2

        # Driver detection state
        self.tmc = None
        self.driver_type = None   # 'tmc2240', 'tmc2209', 'tmc5160', etc.
        self.is_2240 = False
        self.is_2209 = False
        self.is_5160 = False
        # SG2 family (TMC5160, TMC2130, TMC2660) — StallGuard2 in SpreadCycle,
        # SG_RESULT in DRV_STATUS bits 0-9, threshold field 'sgt' (signed).
        self.sg2_driver = False
        self.sg4_available = False  # SG4_RESULT register (TMC2240)
        # Trigger threshold profile — replaced in _lookup_tmc with the
        # driver-appropriate subclass. Default = TMC5160 (validated).
        self.profile = TMC5160Profile

        # Sample buffers
        self.samples_sg = []
        self.samples_time = []
        self.sampling_active = False
        self.sample_timer = None
        self.sample_start_time = 0.0

        # Main command
        self.gcode.register_command(
            'TMC_FLOW_FIND_MAX', self.cmd_TMC_FLOW_FIND_MAX,
            desc='Find max volumetric flow rate via StallGuard slip '
                 'detection (Coarse → Bisection → Verification)')
        self.gcode.register_command(
            'TMC_FLOW_STATUS', self.cmd_TMC_FLOW_STATUS,
            desc='Show current TMC StallGuard diagnostic values')
        self.gcode.register_command(
            'TMC_FLOW_TEST_SG_VARIANTS',
            self.cmd_TMC_FLOW_TEST_SG_VARIANTS,
            desc='TMC2209-only diagnostic: probe SG_RESULT in both '
                 'StealthChop and SpreadCycle to determine empirically '
                 'which chopper mode produces a usable signal on this '
                 'specific hardware')

    # ─── TMC driver lookup ──────────────────────────────────────────

    def _lookup_tmc(self):
        if self.tmc is not None:
            return
        candidates = ['tmc2240', 'tmc5160', 'tmc2209', 'tmc2226',
                      'tmc2130', 'tmc2208', 'tmc2660']
        for drv in candidates:
            obj_name = '%s %s' % (drv, self.stepper_name)
            tmc = self.printer.lookup_object(obj_name, None)
            if tmc is not None:
                self.tmc = tmc
                self.driver_type = drv
                self.is_2240 = (drv == 'tmc2240')
                self.is_2209 = (drv == 'tmc2209')
                self.is_5160 = (drv == 'tmc5160')
                # TMC2240 is treated as an SG2 driver (SpreadCycle path).
                # Its SG4/StealthChop path delivers significantly less
                # torque at high speeds — confirmed empirically: the same
                # Sherpa Mini extruder reaches ~110 mm³/s on TMC5160 SG2
                # but only ~60 mm³/s on TMC2240 SG4. Since this plugin
                # exists to find MAX flow, we always use the high-torque
                # SG2 path on TMC2240.
                self.sg2_driver = drv in ('tmc5160', 'tmc2130',
                                          'tmc2660', 'tmc2240')
                # Pick the trigger threshold profile for this driver
                # family. Each profile is independently tunable; the
                # TMC5160 profile is the validated baseline.
                self.profile = get_trigger_profile(drv)
                # Check for SG4_RESULT register (TMC2240 only)
                try:
                    self.sg4_available = (
                        self.is_2240
                        and 'SG4_RESULT' in self.tmc.mcu_tmc.name_to_reg)
                except AttributeError:
                    self.sg4_available = False
                logging.info(
                    "tmc_flow_test: using %s for stepper '%s' "
                    "(SG4=%s, is_2209=%s, sg2=%s, profile=%s)",
                    drv, self.stepper_name, self.sg4_available,
                    self.is_2209, self.sg2_driver,
                    self.profile.__name__)
                return
        raise self.gcode.error(
            "tmc_flow_test: no TMC driver found for stepper '%s'."
            % self.stepper_name)

    # ─── Driver-specific field accessors ────────────────────────────

    def _get_sg_threshold_field_name(self):
        """Return the correct SG-threshold field name for this driver.

        TMC5160 / TMC2130 / TMC2660 / TMC2240 (SG2 path) use 'sgt'.
        TMC2209 uses 'sgthrs'.
        """
        if self.sg2_driver:
            # TMC5160 / TMC2130 / TMC2660 / TMC2240 — StallGuard2 threshold
            # (signed -64..63)
            return 'sgt'
        # TMC2209, TMC2226, TMC2208 use 'sgthrs'
        return 'sgthrs'

    def _get_sg_label(self):
        """Human-readable label for the SG signal.

        TMC2240 in SG2 mode uses SG_RESULT in DRV_STATUS just like the
        TMC5160 — the SG4_RESULT register is unused on the SG2 path.
        """
        return 'SG_RESULT'

    # ─── TMC config validation ──────────────────────────────────────

    def _check_tmc_config(self):
        """Verify TMC driver is configured correctly for the SG-based test.

        Returns (problems, infos).
        """
        problems = []
        infos = []
        if self.tmc is None:
            return ([('tmc', None, 'No TMC driver found')], infos)

        def get(name):
            try:
                return self.tmc.fields.get_field(name)
            except (KeyError, AttributeError):
                return None

        tpwmthrs = get('tpwmthrs')
        tcoolthrs = get('tcoolthrs')
        semin = get('semin')
        en_pwm_mode = get('en_pwm_mode')
        en_spread_cycle = get('en_spreadCycle')
        sg_thrs_field = self._get_sg_threshold_field_name()
        sg_thrs_val = get(sg_thrs_field)

        # ─── Chopper-mode check (driver-specific) ───
        # SG4 family (TMC2209 only now): StallGuard4 needs StealthChop ON
        # SG2 family (TMC5160, TMC2130, TMC2660, TMC2240):
        #   StallGuard2 needs SpreadCycle
        if self.is_2209:
            stealthchop_active = True
            stealthchop_indicator = None
            if en_spread_cycle is not None:
                stealthchop_active = (en_spread_cycle == 0)
                stealthchop_indicator = (
                    'en_spreadCycle', en_spread_cycle, 'should be 0')

            if not stealthchop_active and stealthchop_indicator:
                problems.append(
                    (stealthchop_indicator[0], stealthchop_indicator[1],
                     'StealthChop is not active. StallGuard4 needs '
                     'StealthChop ON.\n'
                     'Add to your [%s extruder] section:\n'
                     '  stealthchop_threshold: 999999' % self.driver_type))
            elif (tpwmthrs is not None and tpwmthrs > 0
                  and tpwmthrs < 0x10000):
                # Mid-range tpwmthrs would switch to SpreadCycle at higher
                # speeds.
                problems.append(
                    ('tpwmthrs', tpwmthrs,
                     'StealthChop only active below a velocity threshold '
                     '(tpwmthrs=%d). At higher flows the driver switches '
                     'to SpreadCycle and breaks StallGuard4.\n'
                     'Set:\n'
                     '  stealthchop_threshold: 999999' % tpwmthrs))
        elif self.sg2_driver:
            # TMC5160 / TMC2130 / TMC2660 / TMC2240 — SG2 requires SpreadCycle.
            #
            # Klipper's tmc.py writes tpwmthrs = 0xFFFFF (= 1048575) as the
            # default when stealthchop_threshold is NOT set in the config —
            # together with en_pwm_mode=0 this is the "SpreadCycle at all
            # speeds" state we actually want for SG2. So:
            #
            #   • en_pwm_mode is the authoritative signal: 0 = SpreadCycle.
            #   • Only flag tpwmthrs when en_pwm_mode is 1 AND tpwmthrs is in
            #     the mid-range (0 < tpwmthrs < 0xFFFFF), which would make
            #     the driver switch chopper mode based on velocity.
            #   • tpwmthrs == 0xFFFFF means "never enter StealthChop" and is
            #     the Klipper default → not a problem.
            stealthchop_default_tpwmthrs = 0xFFFFF  # 1048575
            if en_pwm_mode is not None and en_pwm_mode == 1:
                # Build TMC2240-specific advice if applicable
                if self.is_2240:
                    extra = (
                        '\nNote: this plugin uses TMC2240 in '
                        'SG2/SpreadCycle mode for max torque (~50 %% '
                        'higher peak flow than SG4/StealthChop). If you '
                        'use sensorless homing, set the chopper mode '
                        'temporarily for the test only.')
                else:
                    extra = ''
                if (tpwmthrs is not None and 0 < tpwmthrs
                        < stealthchop_default_tpwmthrs):
                    problems.append(
                        ('tpwmthrs', tpwmthrs,
                         '%s StallGuard2 needs SpreadCycle at all speeds, '
                         'but tpwmthrs=%d enables StealthChop below that '
                         'velocity.\n'
                         'Remove stealthchop_threshold from your [%s '
                         'extruder] section (or set it to 0).%s'
                         % (self.driver_type.upper(), tpwmthrs,
                            self.driver_type, extra)))
                else:
                    problems.append(
                        ('en_pwm_mode', en_pwm_mode,
                         'StealthChop is active. %s StallGuard2 only works '
                         'in SpreadCycle mode.\n'
                         'Remove stealthchop_threshold from your [%s '
                         'extruder] section (do not set it to 0 — remove '
                         'the line).%s'
                         % (self.driver_type.upper(), self.driver_type,
                            extra)))
            # else: en_pwm_mode == 0 → SpreadCycle is active. tpwmthrs is
            # irrelevant in this case (the driver never enters StealthChop),
            # whether it's 0 or 0xFFFFF.

        # ─── tcoolthrs check (StallGuard gate) ───
        if tcoolthrs == 0:
            problems.append(
                ('tcoolthrs', tcoolthrs,
                 'StallGuard reading disabled (tcoolthrs=0).\n'
                 'Add to your [%s extruder] section:\n'
                 '  coolstep_threshold: 0.5' % self.driver_type))

        # ─── SG threshold check ───
        # Only enforced for TMC2209 (SG4 path). For SG2 drivers
        # (TMC5160/2130/2660/2240), sgt is signed (-64..63) and
        # SG_RESULT can be read regardless of sgt; we don't depend on
        # the hardware stop trigger.
        if self.is_2209 and sg_thrs_val == 0:
            problems.append(
                (sg_thrs_field, sg_thrs_val,
                 'SGTHRS is 0. StallGuard trigger inactive.\n'
                 'Add to your [tmc2209 extruder] section:\n'
                 '  driver_SGTHRS: 100'))

        # ─── CoolStep informational check ───
        # The plugin runs SG-only triggers. CoolStep can still be enabled
        # in the user's config (it doesn't break the test) but it may
        # dampen SG signal during high-load steps and produce a less
        # conservative max-flow result.
        if semin is not None and semin > 0:
            infos.append(
                "Note: CoolStep is active (driver_SEMIN=%d). The test "
                "will run with your configured SEMIN, but CoolStep may "
                "dampen SG signal during high-load steps. For the most "
                "conservative max-flow result, set driver_SEMIN: 0 "
                "temporarily." % semin)
        else:
            infos.append(
                "CoolStep is disabled (driver_SEMIN=0). Motor runs at "
                "constant IRUN — recommended for clean SG signal.")

        # Driver info
        if self.is_2209:
            infos.append(
                "Driver: TMC2209 detected (uses SG_RESULT, sgthrs).")
        elif self.is_2240:
            infos.append(
                "Driver: TMC2240 detected — using SG2/SpreadCycle path "
                "(SG_RESULT via DRV_STATUS, sgt threshold) for max "
                "torque. SG4/StealthChop path is intentionally bypassed "
                "since it delivers ~50%% less peak flow.")
        elif self.sg2_driver:
            infos.append(
                "Driver: %s detected (StallGuard2 in SpreadCycle, "
                "SG_RESULT via DRV_STATUS, sgt threshold)."
                % self.driver_type.upper())

        return (problems, infos)

    # ─── SG sampling ─────────────────────────────────────────────────

    def _read_sg(self):
        """Read StallGuard value directly from the driver register.

        TMC2209: SG_RESULT register (dedicated SG4 register)
        TMC5160 / TMC2130 / TMC2660 / TMC2240 (SG2 path):
            DRV_STATUS bits 0-9 (sg_result field)
        Other drivers: fallback via get_status()

        Note: TMC2240 has a separate SG4_RESULT register which we
        deliberately ignore — we run TMC2240 in SpreadCycle/SG2 mode
        for max torque, and SG2 lives in DRV_STATUS just like TMC5160.
        """
        # TMC2209 has its own SG_RESULT register (SG4)
        if self.is_2209:
            try:
                reg_val = self.tmc.mcu_tmc.get_register('SG_RESULT')
                return reg_val & 0x3FF
            except Exception as e:
                logging.debug(
                    "tmc_flow_test: SG_RESULT read failed: %s", e)
                return None

        # SG2 family (TMC5160 / TMC2130 / TMC2660 / TMC2240):
        # SG_RESULT is in DRV_STATUS bits 0-9
        if self.sg2_driver:
            try:
                reg_val = self.tmc.mcu_tmc.get_register('DRV_STATUS')
                return reg_val & 0x3FF
            except Exception as e:
                logging.debug(
                    "tmc_flow_test: DRV_STATUS read failed: %s", e)
                return None

        # Fallback for any other unknown driver
        try:
            drv = self.tmc.get_status(self.reactor.monotonic())
            if 'drv_status' in drv and isinstance(drv['drv_status'], dict):
                return drv['drv_status'].get('sg_result')
            return drv.get('sg_result')
        except Exception:
            return None

    def _start_sampling(self):
        self.samples_sg = []
        self.samples_time = []
        # Thermal samples are taken at the same cadence as SG, so we
        # only capture data DURING active extrusion. The first thermal
        # sample at t=0 establishes the baseline; later samples show
        # the heater's response to the load.
        self.samples_thermal = []
        self.sample_start_time = self.reactor.monotonic()
        self.sampling_active = True
        # Cadence for thermal sampling — SG sampling is 20 Hz which is
        # overkill for thermal. Sample thermal every Nth SG sample.
        self._thermal_sample_counter = 0
        self.sample_timer = self.reactor.register_timer(
            self._sample_callback, self.reactor.NOW)

    def _stop_sampling(self):
        self.sampling_active = False
        if self.sample_timer is not None:
            self.reactor.unregister_timer(self.sample_timer)
            self.sample_timer = None

    def _sample_callback(self, eventtime):
        if not self.sampling_active:
            return self.reactor.NEVER
        sg = self._read_sg()
        rel_t = eventtime - self.sample_start_time
        # For drivers we read directly from registers, accept any non-None
        # value (including 0). For fallback drivers via get_status, we
        # filter 0/None as those usually mean "not yet polled".
        direct_read = self.is_2209 or self.sg2_driver
        if sg is not None:
            if direct_read or sg > 0:
                self.samples_sg.append(sg)
                self.samples_time.append(rel_t)
        # Thermal sampling at lower cadence (every 5th SG sample = ~4 Hz)
        # — temp + PWM don't change fast enough to need 20 Hz.
        self._thermal_sample_counter += 1
        if self._thermal_sample_counter >= 5:
            self._thermal_sample_counter = 0
            self.samples_thermal.append(self._get_thermal_snapshot())
        return eventtime + SAMPLE_INTERVAL

    # ─── Statistics ─────────────────────────────────────────────────

    @staticmethod
    def _stats(samples):
        """Median + IQR + basic stats. Returns None if empty."""
        if not samples:
            return None
        sorted_s = sorted(samples)
        n = len(sorted_s)

        def percentile(p):
            if n == 1:
                return sorted_s[0]
            k = (n - 1) * p / 100.0
            f = int(k)
            c = min(f + 1, n - 1)
            return sorted_s[f] + (k - f) * (sorted_s[c] - sorted_s[f])

        return {
            'min': sorted_s[0], 'max': sorted_s[-1],
            'avg': sum(sorted_s) / n,
            'median': percentile(50),
            'p25': percentile(25),
            'p75': percentile(75),
            'std': _pstdev(sorted_s) if n > 1 else 0.0,
            'n': n,
        }

    # ─── CSV / HTML output ──────────────────────────────────────────

    def _write_csv(self, path, results, meta):
        with open(path, 'w') as f:
            f.write("# TMC Flow Test v%s results\n" % MODULE_VERSION)
            f.write("# Plugin by Steven (Fragmon) — Crydteam\n")
            f.write("# YouTube: https://www.youtube.com/@crydteamprinting\n")
            tmc_settings = None
            for k, v in meta.items():
                if k == 'tmc_settings':
                    tmc_settings = v
                    continue
                f.write("# %s: %s\n" % (k, v))
            # TMC settings as a separate, readable block at the end
            # of the comment header — useful as a paper trail.
            if tmc_settings:
                f.write("#\n# TMC driver settings at test start:\n")
                for label, value, raw in tmc_settings:
                    f.write("#   %s = %s  [%s]\n" % (label, value, raw))

            # CSV header
            f.write("phase,flow_mm3s,sg_median,sg_p25,sg_p75,sg_avg,"
                    "sg_min,sg_max,sg_n,n_repeats,sg_run_cv_pct,"
                    "run_sg_avgs,"
                    "temp_target,temp_start,temp_end,temp_min,"
                    "temp_avg,temp_drop,"
                    "pwm_min,pwm_max,pwm_avg,"
                    "tmc_otpw,tmc_ot\n")

            for r in results:
                sg = r.get('sg') or {}
                rc = r.get('run_consistency') or {}
                run_sg = r.get('run_sg_avgs') or []
                phase = r.get('phase', 'coarse')
                th = r.get('thermal') or {}

                def fmt(d, key):
                    v = d.get(key, '')
                    if isinstance(v, float):
                        return "%.1f" % v
                    return str(v)

                def fmt_t(d, key, dec=1):
                    v = d.get(key)
                    if v is None:
                        return ''
                    return "%.*f" % (dec, v)

                def fmt_pwm(d, key):
                    v = d.get(key)
                    if v is None:
                        return ''
                    return "%.3f" % v

                def fmt_flag(d, key):
                    v = d.get(key)
                    if v is None:
                        return ''
                    return "%d" % v

                f.write("%s,%.2f,%s,%s,%s,%s,%s,%s,%s,"
                        "%d,%s,%s,"
                        "%s,%s,%s,%s,%s,%s,"
                        "%s,%s,%s,"
                        "%s,%s\n" % (
                    phase,
                    r['flow'],
                    fmt(sg, 'median'), fmt(sg, 'p25'), fmt(sg, 'p75'),
                    fmt(sg, 'avg'), sg.get('min', ''), sg.get('max', ''),
                    sg.get('n', 0),
                    len(run_sg),
                    "%.1f" % rc.get('sg_cv', 0) if rc else '',
                    '|'.join("%.1f" % v for v in run_sg),
                    fmt_t(th, 'temp_target'),
                    fmt_t(th, 'temp_start'),
                    fmt_t(th, 'temp_end'),
                    fmt_t(th, 'temp_min'),
                    fmt_t(th, 'temp_avg'),
                    fmt_t(th, 'temp_drop', 2),
                    fmt_pwm(th, 'pwm_min'),
                    fmt_pwm(th, 'pwm_max'),
                    fmt_pwm(th, 'pwm_avg'),
                    fmt_flag(th, 'tmc_otpw_any'),
                    fmt_flag(th, 'tmc_ot_any'),
                ))

    def _write_html(self, path, results, meta, limit_reason,
                    final_result=None):
        """Compact HTML report with chart.

        final_result (optional dict): when present, renders the prominent
        result panel with the optimal flow value. Expected keys:
            max_safe (float)         -- mm³/s, the optimal value
            verify_cv (float)        -- run-to-run CV in percent
            quality (str)            -- e.g. 'good (stable)'
            stop_reason (str|None)   -- last trigger reason (optional)
            trigger_events (list)    -- chronological trigger / borderline
                                         / verify-fail events for the
                                         decision-trail panel
            baseline_stats (dict)    -- "healthy" CV/IQR ranges from the
                                         coarse phase (used by the
                                         decision-trail and chart bands)
        """
        # ─── Build chart datasets (sorted by flow rate) ─────────────
        # Test phases run NOT in flow order: bisection revisits lower
        # flows after a coarse trigger, verification re-tests the safe
        # value. Plotting in time order makes the line zigzag and
        # confuses interpretation. We therefore:
        #   1. Deduplicate by flow — when the same flow appears multiple
        #      times (e.g. verify after bisect at the same flow), the
        #      LATEST entry wins (it's the most-confirmed measurement).
        #   2. Sort by flow ascending.
        # The Test Details table below keeps chronological order so the
        # decision flow is still inspectable.
        results_by_flow = {}
        for r in results:
            results_by_flow[r['flow']] = r  # later entry overwrites
        results_for_charts = sorted(results_by_flow.values(),
                                     key=lambda r: r['flow'])

        flows = [r['flow'] for r in results_for_charts]
        phases = [r.get('phase', 'coarse') for r in results_for_charts]
        sg_label = self._get_sg_label()
        sg_median = [r['sg']['median'] if r['sg'] else None
                     for r in results_for_charts]
        sg_p25 = [r['sg']['p25'] if r['sg'] else None
                  for r in results_for_charts]
        sg_p75 = [r['sg']['p75'] if r['sg'] else None
                  for r in results_for_charts]
        sg_avg = [r['sg']['avg'] if r['sg'] else None
                  for r in results_for_charts]

        # Thermal data extraction — all values may be None per step.
        # The chart's JS code checks if any non-null exists before
        # rendering the chart at all.
        def th(r, key):
            t = r.get('thermal') or {}
            return t.get(key)
        temp_actual = [th(r, 'temp_avg') for r in results_for_charts]
        temp_target = [th(r, 'temp_target') for r in results_for_charts]
        temp_min    = [th(r, 'temp_min') for r in results_for_charts]
        temp_drop   = [th(r, 'temp_drop') for r in results_for_charts]
        pwm_avg     = [th(r, 'pwm_avg') for r in results_for_charts]
        pwm_max     = [th(r, 'pwm_max') for r in results_for_charts]
        tmc_otpw    = [th(r, 'tmc_otpw_any') for r in results_for_charts]
        tmc_ot      = [th(r, 'tmc_ot_any') for r in results_for_charts]

        # Intra-run drift load (Phase 3 thermal indicator)
        def ir(r, key):
            v = r.get('intra_run') or {}
            return v.get(key)
        intra_drift = [ir(r, 'mean_drift_load') for r in results_for_charts]

        # CV per step (run-to-run variance). One of the strongest slip
        # signals. Used by the variance bar chart in the new layout.
        cv_data = []
        for r in results_for_charts:
            rc = r.get('run_consistency') or {}
            cv_data.append(rc.get('sg_cv'))

        # Linear feed speed and residence time per step. Computed from
        # melt_zone_length and filament_area. Both purely informational
        # — no triggers depend on them.
        linear_speeds = []
        residence_times = []
        for r in results_for_charts:
            flow = r['flow']
            if self.filament_area > 0:
                ls = flow / self.filament_area  # mm/s
                linear_speeds.append(ls)
                if ls > 0 and self.melt_zone_length > 0:
                    residence_times.append(self.melt_zone_length / ls)
                else:
                    residence_times.append(None)
            else:
                linear_speeds.append(None)
                residence_times.append(None)

        if final_result is not None:
            max_safe = final_result.get('max_safe')
            verify_cv = final_result.get('verify_cv', 0.0)
            quality = final_result.get('quality', '')
            stop_reason = final_result.get('stop_reason')
            trigger_events = final_result.get('trigger_events') or []
            baseline_stats = final_result.get('baseline_stats')

            stop_html = ''
            if stop_reason:
                stop_html = (
                    '<div class="stop-line">Stop trigger that ended the '
                    'search: <em>%s</em></div>' % stop_reason)

            summary_html = (
                '<div class="summary final">'
                '<h2>Maximum Safe Volumetric Flow</h2>'
                '<div class="big-number">%.1f<span class="unit">'
                'mm³/s</span></div>'
                '<p class="result-explainer">'
                'This is the highest extrusion speed (in cubic millimeters '
                'of plastic per second) where your motor still '
                'reliably grips the filament. Beyond this point, the '
                'extruder gear starts to slip on the filament — '
                'producing under-extrusion, layer issues or print '
                'failures.'
                '</p>'
                '<div class="quality-line">Verification quality: '
                '<strong>%s</strong> '
                '<span class="quality-tooltip" title="How consistent the '
                'measurement was across the 5 verify repetitions. Lower '
                'CV = more consistent = more trust in the result.">'
                '(CV = %.1f%%) ⓘ</span></div>'
                '<div class="recommendations">'
                '<h3>Use these values in your slicer</h3>'
                '<div class="rec-table">'
                '<span class="rec-label">Conservative (80%%)</span>'
                '<span class="rec-value">%.1f mm³/s</span>'
                '<span class="rec-note">recommended for slicer</span>'
                '<span class="rec-label">Aggressive (90%%)</span>'
                '<span class="rec-value">%.1f mm³/s</span>'
                '<span class="rec-note">only with margin</span>'
                '</div>'
                '<p class="rec-explainer">'
                'The 80%% value is what most people should use as their '
                'slicer\'s "max volumetric speed" — it leaves headroom '
                'for filament variability, temperature fluctuations and '
                'long prints. The 90%% value is more aggressive and '
                'should only be used after long-term print testing.'
                '</p>'
                '</div>'
                '%s'
                '</div>'
                % (max_safe, quality, verify_cv,
                   max_safe * 0.8, max_safe * 0.9, stop_html))
        elif limit_reason:
            summary_html = (
                '<div class="summary"><h2>Result</h2>'
                '<p>Stop reason: <strong>%s</strong></p></div>'
                % limit_reason)
            trigger_events = []
            baseline_stats = None
            max_safe = None
        else:
            summary_html = (
                '<div class="summary"><h2>Result</h2>'
                '<p>Test completed without trigger.</p></div>')
            trigger_events = []
            baseline_stats = None
            max_safe = None

        # ─── Decision-trail panel: WHY the result is what it is ───
        decision_trail_html = ''
        if trigger_events or baseline_stats:
            kind_labels = {
                'trigger': ('🚨', 'TRIGGER',
                            'Slip detected — phase ended at this flow'),
                'borderline-retest': ('🟡', 'BORDERLINE',
                                      'In gray zone — re-measured for '
                                      'confirmation'),
                'borderline-confirmed': ('🚨', 'BORDERLINE CONFIRMED',
                                         'Re-test also borderline — '
                                         'treated as trigger'),
                'verify-borderline': ('🟡', 'VERIFY BORDERLINE',
                                      'Verify in gray zone — re-measured'),
                'verify-fail': ('⚠️', 'VERIFY FAILED',
                                'Verify trigged at this flow — fell back '
                                'into bisection'),
            }
            event_rows = []
            for ev in trigger_events:
                icon, label, _desc = kind_labels.get(
                    ev['kind'], ('•', ev['kind'].upper(), ''))
                metric_bits = []
                if ev.get('cv') is not None:
                    metric_bits.append('CV %.1f%%' % ev['cv'])
                if ev.get('iqr') is not None:
                    metric_bits.append('IQR %.0f' % ev['iqr'])
                if ev.get('sg_median') is not None:
                    metric_bits.append('%s %.0f'
                                       % (sg_label, ev['sg_median']))
                metrics_str = ' · '.join(metric_bits) if metric_bits else '—'
                event_rows.append(
                    '<tr class="ev-%s">'
                    '<td class="ev-icon">%s</td>'
                    '<td class="ev-phase">%s</td>'
                    '<td class="ev-flow">%.1f mm³/s</td>'
                    '<td class="ev-label">%s</td>'
                    '<td class="ev-metrics">%s</td>'
                    '<td class="ev-reason">%s</td>'
                    '</tr>'
                    % (ev['kind'], icon,
                       ev['phase'], ev['flow'], label,
                       metrics_str, ev['reason']))

            # Baseline summary row
            baseline_block = ''
            if baseline_stats:
                baseline_block = (
                    '<div class="baseline-stats">'
                    '<h3>Healthy ranges (from %d coarse-phase steps)</h3>'
                    '<p class="baseline-explainer">'
                    'During the coarse sweep before slip started, the '
                    'plugin saw consistent measurements — CV and IQR '
                    'stayed in the ranges shown below. Once any later '
                    'step jumped <strong>far outside</strong> these '
                    '"healthy" ranges (e.g. CV doubling, IQR widening '
                    'a lot), that was the plugin\'s signal that slip '
                    'had started. Think of it like measuring a '
                    'person\'s normal heart rate at rest — if it '
                    'suddenly doubles, something\'s changed.'
                    '</p>'
                    '<table class="baseline-table">'
                    '<thead><tr><th>Metric</th><th>Min</th><th>Median</th>'
                    '<th>Mean</th><th>Max</th>'
                    '<th>Trigger floor (bisection)</th></tr></thead>'
                    '<tbody>'
                    '<tr><td>Run-to-run CV (%%)</td>'
                    '<td>%.1f</td><td>%.1f</td><td>%.1f</td><td>%.1f</td>'
                    '<td>≥ 4 %% AND ≥ 2.0× median, OR ≥ 7 %% absolute</td>'
                    '</tr>'
                    '<tr><td>IQR (P75 − P25, raw SG units)</td>'
                    '<td>%.0f</td><td>%.0f</td><td>%.0f</td><td>%.0f</td>'
                    '<td>≥ 18 AND ≥ 2.5× median, OR ≥ 25 absolute</td>'
                    '</tr>'
                    '</tbody></table>'
                    '<p class="baseline-legend"><em>'
                    'CV = how much the 5 repetitions of one step varied. '
                    'IQR = how spread out the SG samples were within '
                    'one step. Both are explained in the glossary above.'
                    '</em></p>'
                    '</div>'
                    % (baseline_stats['n_steps'],
                       baseline_stats['cv_min'],
                       baseline_stats['cv_median'],
                       baseline_stats['cv_mean'],
                       baseline_stats['cv_max'],
                       baseline_stats['iqr_min'],
                       baseline_stats['iqr_median'],
                       baseline_stats['iqr_mean'],
                       baseline_stats['iqr_max']))

            events_block = ''
            if event_rows:
                events_block = (
                    '<h3>Decision events (chronological)</h3>'
                    '<p class="trail-explainer">Each event below is a '
                    'point where the plugin detected something unusual '
                    'and changed its decision. The bottom-most '
                    'event-row is what produced the final result.</p>'
                    '<div class="trail-table-wrap">'
                    '<table class="trail-table">'
                    '<thead><tr>'
                    '<th></th><th>Phase</th><th>Flow</th><th>Verdict</th>'
                    '<th>Metrics</th><th>Reason</th>'
                    '</tr></thead><tbody>%s</tbody></table>'
                    '</div>'
                    % ''.join(event_rows))

            decision_trail_html = (
                '<details class="decision-trail" open>'
                '<summary>Why this value? — Decision trail</summary>'
                '<p class="trail-intro">'
                'This section shows <strong>exactly why</strong> the '
                'plugin chose the final value. The "healthy ranges" '
                'table tells you what normal CV/IQR looks like during '
                'the slip-free portion of the test — the plugin '
                'treats anything well outside those ranges as a slip '
                'signal. The events table below lists every '
                'decision moment in chronological order; the '
                'bottom-most row is what produced the final result.'
                '</p>'
                '%s%s'
                '</details>' % (baseline_block, events_block))

        # Build meta info, but skip the tmc_settings list (rendered
        # separately below as its own collapsible section).
        meta_html = ''.join(
            '<div><strong>%s:</strong> %s</div>' % (k, v)
            for k, v in meta.items() if k != 'tmc_settings')

        # Build TMC settings block (collapsible <details> for compactness)
        tmc_settings = meta.get('tmc_settings') or []
        if tmc_settings:
            tmc_rows = ''.join(
                '<tr><td style="text-align:left">%s</td>'
                '<td style="text-align:right;font-family:monospace">%s</td>'
                '<td style="text-align:left;color:#888;font-family:monospace">'
                '%s</td></tr>'
                % (label, value, raw)
                for label, value, raw in tmc_settings)
            tmc_settings_html = (
                '<details class="tmc-settings"><summary>'
                'TMC driver settings at test start (%d fields) — '
                'click to expand</summary>'
                '<table class="tmc-settings-table"><thead><tr>'
                '<th style="text-align:left">Setting</th>'
                '<th style="text-align:right">Value</th>'
                '<th style="text-align:left">Field</th>'
                '</tr></thead><tbody>%s</tbody></table>'
                '</details>'
                % (len(tmc_settings), tmc_rows))
        else:
            tmc_settings_html = ''

        # Data table
        rows = []
        for r in results:
            sg = r.get('sg') or {}
            rc = r.get('run_consistency') or {}
            cv_str = ''
            cv_class = ''
            if rc and 'sg_cv' in rc:
                cv = rc['sg_cv']
                cv_str = "%.1f%%" % cv
                if cv > 25:
                    cv_class = ' style="background:#ffcdd2"'
                elif cv > 10:
                    cv_class = ' style="background:#fff9c4"'

            def fmt(d, key, fs="%.1f"):
                v = d.get(key)
                if v is None or v == '':
                    return '-'
                return fs % v

            rows.append(
                "<tr><td>%.1f</td><td><b>%s</b></td>"
                "<td>%s</td><td>%s</td><td>%s</td>"
                "<td>%d</td><td%s>%s</td></tr>" % (
                    r['flow'], fmt(sg, 'median'),
                    fmt(sg, 'p25'), fmt(sg, 'p75'), fmt(sg, 'avg'),
                    sg.get('n', 0), cv_class, cv_str or '-'))

        table_header = (
            "<th>Flow (mm³/s)</th><th>%s median</th>"
            "<th>%s P25</th><th>%s P75</th><th>%s avg</th>"
            "<th>n</th><th>Inter-run CV</th>"
            % (sg_label, sg_label, sg_label, sg_label))

        table = ("<table><thead><tr>" + table_header
                 + "</tr></thead><tbody>"
                 + "".join(rows) + "</tbody></table>")

        # ─── NEW LAYOUT: Build hero, insights and timeline blocks ────

        # Hero block: big number, slicer recommendations, status pill
        if max_safe is not None:
            verify_cv_for_hero = (final_result.get('verify_cv', 0.0)
                                  if final_result else 0.0)
            verify_repeats_for_hero = (final_result.get('verify_repeats', 0)
                                       if final_result else 0)
            quality_for_hero = (final_result.get('quality', '')
                                if final_result else '')
            status_class = 'success'
            status_text = 'Verified — CV %.1f%% over %d runs' % (
                verify_cv_for_hero, verify_repeats_for_hero)
            if quality_for_hero in ('borderline', 'fragile'):
                status_class = 'warning'
            hero_html = (
                '<div class="hero">'
                '<div class="hero-grid">'
                '<div>'
                '<div class="hero-label">Maximum Safe Flow</div>'
                '<div class="hero-value">%.0f<span class="unit">mm³/s'
                '</span></div>'
                '<div class="hero-status %s"><span>%s</span></div>'
                '</div>'
                '<div class="slicer-recos">'
                '<div class="reco-card conservative">'
                '<div class="reco-percent">80%%</div>'
                '<div class="reco-value">%.0f</div>'
                '<div class="reco-label">conservative<br>everyday use</div>'
                '</div>'
                '<div class="reco-card aggressive">'
                '<div class="reco-percent">90%%</div>'
                '<div class="reco-value">%.0f</div>'
                '<div class="reco-label">aggressive<br>after validation</div>'
                '</div>'
                '</div>'
                '</div>'
                '</div>'
                % (max_safe, status_class, status_text,
                   max_safe * 0.8, max_safe * 0.9))
        else:
            stop_text = limit_reason or 'Test ended without verified result'
            hero_html = (
                '<div class="hero">'
                '<div class="hero-grid">'
                '<div>'
                '<div class="hero-label">Test Result</div>'
                '<div class="hero-value" style="font-size:48px">No verified value</div>'
                '<div class="hero-status warning"><span>%s</span></div>'
                '</div>'
                '<div></div>'
                '</div>'
                '</div>'
                % stop_text)

        # Insights cards: 4 quick-glance summaries
        # 1) Result Quality
        if max_safe is not None:
            verify_cv_ic = (final_result.get('verify_cv', 0.0)
                            if final_result else 0.0)
            quality_ic = (final_result.get('quality', '')
                          if final_result else '')
            verify_n_ic = (final_result.get('verify_repeats', 0)
                           if final_result else 0)
            quality_card_class = 'success'
            quality_value = 'Verified'
            if quality_ic in ('borderline', 'fragile'):
                quality_card_class = 'warning'
                quality_value = quality_ic.title()
            elif quality_ic == '':
                quality_value = 'Done'
            quality_card = (
                '<div class="insight-card %s">'
                '<div class="insight-icon">✓</div>'
                '<div class="insight-title">Result Quality</div>'
                '<div class="insight-value">%s</div>'
                '<div class="insight-detail">CV %.1f%% across %d verify '
                'runs</div></div>'
                % (quality_card_class, quality_value, verify_cv_ic,
                   verify_n_ic))
        else:
            quality_card = (
                '<div class="insight-card warning">'
                '<div class="insight-icon">!</div>'
                '<div class="insight-title">Result Quality</div>'
                '<div class="insight-value">Inconclusive</div>'
                '<div class="insight-detail">No verified value</div></div>')

        # 2) First Trigger
        first_trig_event = None
        for ev in (trigger_events or []):
            if ev.get('kind') == 'trigger':
                first_trig_event = ev
                break
        if first_trig_event:
            trig_metrics = []
            if first_trig_event.get('cv') is not None:
                trig_metrics.append('CV %.1f%%' % first_trig_event['cv'])
            if first_trig_event.get('iqr') is not None:
                trig_metrics.append('IQR %.0f' % first_trig_event['iqr'])
            trig_metric_str = ' · '.join(trig_metrics) if trig_metrics else '—'
            trigger_card = (
                '<div class="insight-card info">'
                '<div class="insight-icon">⊘</div>'
                '<div class="insight-title">First Trigger</div>'
                '<div class="insight-value">%.0f mm³/s</div>'
                '<div class="insight-detail">%s</div></div>'
                % (first_trig_event['flow'], trig_metric_str))
        else:
            trigger_card = (
                '<div class="insight-card info">'
                '<div class="insight-icon">⊘</div>'
                '<div class="insight-title">First Trigger</div>'
                '<div class="insight-value">— </div>'
                '<div class="insight-detail">no trigger fired</div></div>')

        # 3) Thermal Watch
        max_pwm = max((p for p in pwm_max if p is not None), default=None)
        max_drop = max((d for d in temp_drop if d is not None), default=None)
        any_otpw = any(tmc_otpw)
        any_ot = any(tmc_ot)
        # Cold extrusion onset for the insight card. The user-supplied
        # hint takes precedence over auto-detection (visual observation
        # is more reliable than the heater-PWM heuristic).
        cold_extrusion_hint_val = meta.get('cold_extrusion_hint', 0)
        if cold_extrusion_hint_val > 0:
            cold_onset_for_card = cold_extrusion_hint_val
            cold_onset_source = 'user'
        else:
            cold_onset_for_card = self._detect_cold_extrusion_onset(results)
            cold_onset_source = 'auto'

        # Compute peak stress score for the insight card
        score_pairs = self._compute_thermal_stress_per_step(results)
        score_values = [s for _, s in score_pairs if s is not None]
        peak_stress = max(score_values) if score_values else None

        if max_pwm is None and max_drop is None:
            thermal_card = (
                '<div class="insight-card info">'
                '<div class="insight-icon">∅</div>'
                '<div class="insight-title">Thermal Watch</div>'
                '<div class="insight-value">No data</div>'
                '<div class="insight-detail">heater readings unavailable'
                '</div></div>')
        else:
            # The stress-score-based assessment is the PRIMARY indicator.
            # Specific PWM/drop/driver flags add secondary detail.
            if peak_stress is not None:
                if peak_stress < 30:
                    therm_class = 'success'
                    therm_label = 'Stable'
                elif peak_stress < 60:
                    therm_class = 'warning'
                    therm_label = 'Moderate stress'
                else:
                    therm_class = 'danger'
                    therm_label = 'High thermal stress'
            else:
                therm_class = 'success'
                therm_label = 'Stable'

            therm_detail_bits = []
            if peak_stress is not None:
                therm_detail_bits.append('peak stress %.0f/100' % peak_stress)
            if cold_onset_for_card is not None:
                source_tag = ' (user)' if cold_onset_source == 'user' else ''
                therm_detail_bits.append(
                    'onset @ %.0f mm³/s%s' % (cold_onset_for_card, source_tag))
            if max_pwm is not None and max_pwm >= 0.85:
                therm_detail_bits.append('PWM peak %.0f%%' % (max_pwm * 100))
            if max_drop is not None and max_drop >= 1.5:
                therm_detail_bits.append('drop %.1f °C' % max_drop)
            # Recovery deficit: did the heater catch up between reps?
            # If not, that's an even stronger thermal-saturation signal
            # than the drop during a single extrusion.
            recovery_deficits = [
                (r.get('thermal') or {}).get('recovery_deficit_max')
                for r in results]
            recovery_deficits = [d for d in recovery_deficits if d is not None]
            max_recovery = max(recovery_deficits) if recovery_deficits else None
            if max_recovery is not None and max_recovery >= 1.0:
                therm_detail_bits.append(
                    'recovery gap %.1f °C' % max_recovery)
                if max_recovery >= 3.0 and therm_class == 'success':
                    therm_class = 'warning'
                    therm_label = 'Heater not recovering'
            # Override label if driver thermal flags were raised
            if any_ot:
                therm_class = 'danger'
                therm_label = 'Driver overheated'
            elif any_otpw and therm_class == 'success':
                therm_class = 'warning'
                therm_label = 'Driver hot (≥120 °C)'
            therm_detail = (' · '.join(therm_detail_bits)
                            if therm_detail_bits else 'no concerns')
            thermal_card = (
                '<div class="insight-card %s">'
                '<div class="insight-icon">⚠</div>'
                '<div class="insight-title">Thermal Watch</div>'
                '<div class="insight-value">%s</div>'
                '<div class="insight-detail">%s</div></div>'
                % (therm_class, therm_label, therm_detail))

        # 4) Driver Config — concise summary
        driver_label_short = self.driver_type.upper()
        sg_variant = 'SG2' if self.sg2_driver else 'SG4'
        chopper_mode = 'SpreadCycle'  # default for our supported configs
        try:
            if self.tmc is not None:
                if self.is_2209:
                    en_sc = self.tmc.fields.get_field('en_spreadcycle')
                    chopper_mode = ('SpreadCycle' if en_sc == 1
                                    else 'StealthChop')
                else:
                    en_pwm = self.tmc.fields.get_field('en_pwm_mode')
                    chopper_mode = ('StealthChop' if en_pwm == 1
                                    else 'SpreadCycle')
        except (KeyError, AttributeError):
            pass
        # Try to grab a few key values from tmc_settings for the detail line
        tmc_settings_dict = {raw: value for label, value, raw
                              in (meta.get('tmc_settings') or [])}
        detail_bits = []
        if 'sgt' in tmc_settings_dict:
            detail_bits.append('SGT=%s' % tmc_settings_dict['sgt'])
        if 'sgthrs' in tmc_settings_dict:
            detail_bits.append('SGTHRS=%s' % tmc_settings_dict['sgthrs'])
        if 'irun' in tmc_settings_dict:
            detail_bits.append('IRUN=%s' % tmc_settings_dict['irun'])
        config_detail = ', '.join(detail_bits) if detail_bits else 'see details'
        config_card = (
            '<div class="insight-card info">'
            '<div class="insight-icon">⚙</div>'
            '<div class="insight-title">Driver Config</div>'
            '<div class="insight-value">%s / %s</div>'
            '<div class="insight-detail">%s · %s</div></div>'
            % (sg_variant, chopper_mode, driver_label_short, config_detail))

        insights_html = (
            '<div class="insights">%s%s%s%s</div>'
            % (quality_card, trigger_card, thermal_card, config_card))

        # Decision Timeline — friendly summary of phase transitions
        timeline_items = []
        # Group results by phase to produce one entry per coarse-run /
        # bisect-step / verify event.
        coarse_results = [r for r in results if r.get('phase') == 'coarse']
        bisect_results = [r for r in results if r.get('phase') == 'bisect']
        verify_results = [r for r in results if r.get('phase') == 'verify']

        # First coarse summary
        if coarse_results:
            non_trigger_coarse = []
            triggered_coarse = None
            for cr in coarse_results:
                # detect trigger event for this flow+phase
                triggered_here = False
                for ev in (trigger_events or []):
                    if (ev['phase'] == 'coarse'
                            and abs(ev['flow'] - cr['flow']) < 0.01
                            and ev.get('kind') in ('trigger',
                                                   'borderline-confirmed')):
                        triggered_here = True
                        triggered_coarse = (cr, ev)
                        break
                if not triggered_here:
                    non_trigger_coarse.append(cr)
            if non_trigger_coarse:
                f_low = non_trigger_coarse[0]['flow']
                f_high = non_trigger_coarse[-1]['flow']
                first_sg = (non_trigger_coarse[0]['sg'].get('median')
                            if non_trigger_coarse[0].get('sg') else None)
                last_sg = (non_trigger_coarse[-1]['sg'].get('median')
                           if non_trigger_coarse[-1].get('sg') else None)
                last_cv = (non_trigger_coarse[-1].get('run_consistency')
                           or {}).get('sg_cv')
                detail_parts = ['%d coarse steps' % len(non_trigger_coarse)]
                if first_sg is not None and last_sg is not None:
                    detail_parts.append(
                        '%s went from %.0f to %.0f' % (
                            sg_label, first_sg, last_sg))
                if last_cv is not None:
                    detail_parts.append('final CV %.1f%%' % last_cv)
                timeline_items.append(
                    '<div class="timeline-item">'
                    '<div class="timeline-phase coarse">Coarse sweep</div>'
                    '<div class="timeline-content">'
                    '<div class="timeline-flow">%.0f → %.0f mm³/s</div>'
                    '<div class="timeline-detail">%s</div>'
                    '</div></div>'
                    % (f_low, f_high, '. '.join(detail_parts) + '.'))
            if triggered_coarse:
                cr, ev = triggered_coarse
                timeline_items.append(
                    '<div class="timeline-item">'
                    '<div class="timeline-phase trigger">Trigger</div>'
                    '<div class="timeline-content">'
                    '<div class="timeline-flow">%.0f mm³/s — slip detected</div>'
                    '<div class="timeline-detail">%s</div>'
                    '</div></div>'
                    % (cr['flow'], ev.get('reason', 'trigger fired')))

        # Bisection items
        if bisect_results:
            for br in bisect_results:
                cv_b = (br.get('run_consistency') or {}).get('sg_cv')
                # Was this bisect step a borderline-confirmed trigger?
                trig_here = None
                for ev in (trigger_events or []):
                    if (ev['phase'] == 'bisect'
                            and abs(ev['flow'] - br['flow']) < 0.01):
                        trig_here = ev
                        break
                if trig_here:
                    label_text = ('triggered (re-test confirmed)'
                                  if trig_here.get('kind')
                                     == 'borderline-confirmed'
                                  else trig_here.get('kind', 'trigger'))
                    timeline_items.append(
                        '<div class="timeline-item">'
                        '<div class="timeline-phase trigger">Bisect</div>'
                        '<div class="timeline-content">'
                        '<div class="timeline-flow">%.0f mm³/s — %s</div>'
                        '<div class="timeline-detail">%s</div>'
                        '</div></div>'
                        % (br['flow'], label_text,
                           trig_here.get('reason', '')))
                else:
                    cv_str = ('CV %.1f%%' % cv_b) if cv_b is not None else '—'
                    timeline_items.append(
                        '<div class="timeline-item">'
                        '<div class="timeline-phase bisect">Bisect</div>'
                        '<div class="timeline-content">'
                        '<div class="timeline-flow">%.0f mm³/s — passed</div>'
                        '<div class="timeline-detail">%s</div>'
                        '</div></div>'
                        % (br['flow'], cv_str))

        # Verify items
        if verify_results:
            last_verify = verify_results[-1]
            cv_v = (last_verify.get('run_consistency') or {}).get('sg_cv')
            v_failed = False
            for ev in (trigger_events or []):
                if ev.get('kind') == 'verify-fail':
                    v_failed = True
                    break
            if v_failed:
                timeline_items.append(
                    '<div class="timeline-item">'
                    '<div class="timeline-phase trigger">Verify</div>'
                    '<div class="timeline-content">'
                    '<div class="timeline-flow">%.0f mm³/s — verify failed</div>'
                    '<div class="timeline-detail">Re-bisected to find a stable point.</div>'
                    '</div></div>'
                    % last_verify['flow'])
            else:
                cv_str = ('CV %.1f%%' % cv_v) if cv_v is not None else '—'
                n_repeats = len(last_verify.get('run_sg_avgs') or [])
                timeline_items.append(
                    '<div class="timeline-item">'
                    '<div class="timeline-phase verify">Verify</div>'
                    '<div class="timeline-content">'
                    '<div class="timeline-flow">%.0f mm³/s — confirmed</div>'
                    '<div class="timeline-detail">%d repetitions, %s. '
                    'Result is verified.</div>'
                    '</div></div>'
                    % (last_verify['flow'], n_repeats, cv_str))

        if timeline_items:
            timeline_html = (
                '<div class="section">'
                '<div class="section-header">'
                '<div class="section-title">Decision Timeline</div>'
                '<div class="section-meta">why this value</div>'
                '</div>'
                '<div class="timeline">%s</div>'
                '</div>'
                % ''.join(timeline_items))
        else:
            timeline_html = ''

        # Driver label for header
        driver_label_full = '%s / %s' % (self.driver_type, self.stepper_name)

        # Decide CV trigger threshold for the variance bar chart
        cv_trigger_threshold = self.profile.CV_HIGH_VARIANCE

        html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TMC Flow Test — %(timestamp)s</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&family=Manrope:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-annotation@3.0.1"></script>
<style>
:root {
    --bg-primary: #0e1117; --bg-secondary: #161b22;
    --bg-elevated: #1c2128; --bg-card: #21262d;
    --bg-card-hover: #30363d; --border: #30363d;
    --border-bright: #484f58; --text-primary: #e6edf3;
    --text-secondary: #8b949e; --text-tertiary: #6e7681;
    --accent: #58a6ff; --accent-dim: #1f6feb;
    --success: #3fb950; --success-bg: rgba(63, 185, 80, 0.12);
    --warning: #d29922; --warning-bg: rgba(210, 153, 34, 0.12);
    --danger: #f85149; --danger-bg: rgba(248, 81, 73, 0.12);
    --info: #58a6ff; --info-bg: rgba(88, 166, 255, 0.12);
    --font-mono: 'JetBrains Mono', monospace;
    --font-sans: 'Manrope', system-ui, sans-serif;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
    font-family: var(--font-sans); background: var(--bg-primary);
    color: var(--text-primary); line-height: 1.5; min-height: 100vh;
    background-image:
      radial-gradient(at 20%% 0%%, rgba(31, 111, 235, 0.08) 0px, transparent 50%%),
      radial-gradient(at 80%% 0%%, rgba(88, 166, 255, 0.04) 0px, transparent 50%%);
    background-attachment: fixed;
}
.container { max-width: 1280px; margin: 0 auto; padding: 0 24px; }
header {
    border-bottom: 1px solid var(--border); padding: 20px 0; margin-bottom: 32px;
    background: rgba(14, 17, 23, 0.85); backdrop-filter: blur(8px);
    position: sticky; top: 0; z-index: 100;
}
header .container {
    display: flex; align-items: center; justify-content: space-between; gap: 24px;
}
.brand { display: flex; align-items: center; gap: 12px; }
.brand-logo {
    width: 36px; height: 36px; border-radius: 8px;
    background: linear-gradient(135deg, var(--accent) 0%%, var(--accent-dim) 100%%);
    display: grid; place-items: center; font-weight: 800; color: white;
    font-family: var(--font-mono); font-size: 14px; letter-spacing: -0.05em;
}
.brand-text { line-height: 1.2; }
.brand-name { font-weight: 700; font-size: 15px; letter-spacing: -0.01em; }
.brand-subtitle { color: var(--text-tertiary); font-size: 12px; font-weight: 500; }
.header-meta { display: flex; gap: 16px; font-size: 12px;
               color: var(--text-tertiary); font-family: var(--font-mono); }
.header-meta-item { display: flex; align-items: center; gap: 6px; }
.header-meta-item .dot { width: 6px; height: 6px; border-radius: 50%%; background: var(--success); }
.hero {
    background: linear-gradient(135deg, rgba(63, 185, 80, 0.08) 0%%,
                rgba(31, 111, 235, 0.05) 100%%);
    border: 1px solid var(--border); border-radius: 16px;
    padding: 40px 48px; margin-bottom: 32px; position: relative; overflow: hidden;
}
.hero::before {
    content: ''; position: absolute; top: 0; right: 0; width: 240px; height: 240px;
    background: radial-gradient(circle, rgba(63, 185, 80, 0.15) 0%%, transparent 70%%);
    pointer-events: none;
}
.hero-grid { display: grid; grid-template-columns: 1fr 320px; gap: 48px;
             align-items: center; position: relative; }
.hero-label { font-size: 12px; text-transform: uppercase; letter-spacing: 0.15em;
              color: var(--text-tertiary); font-weight: 600; margin-bottom: 8px; }
.hero-value { font-family: var(--font-mono); font-size: 88px; font-weight: 700;
              line-height: 1; letter-spacing: -0.04em; color: var(--text-primary);
              display: flex; align-items: baseline; gap: 12px; margin-bottom: 16px; }
.hero-value .unit { font-size: 24px; color: var(--text-secondary);
                    font-weight: 500; letter-spacing: 0; }
.hero-status {
    display: inline-flex; align-items: center; gap: 8px;
    padding: 6px 12px; background: var(--success-bg);
    border: 1px solid rgba(63, 185, 80, 0.3); border-radius: 999px;
    font-size: 12px; font-weight: 600; color: var(--success);
    text-transform: uppercase; letter-spacing: 0.05em;
}
.hero-status.warning { background: var(--warning-bg);
                       border-color: rgba(210, 153, 34, 0.3); color: var(--warning); }
.hero-status.warning::before { background: var(--warning); box-shadow: 0 0 8px var(--warning); }
.hero-status::before { content: ''; width: 8px; height: 8px; border-radius: 50%%;
                       background: var(--success); box-shadow: 0 0 8px var(--success); }
.slicer-recos { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.reco-card { background: var(--bg-card); border: 1px solid var(--border);
             border-radius: 12px; padding: 16px; text-align: center;
             transition: border-color 0.2s; }
.reco-card:hover { border-color: var(--border-bright); }
.reco-percent { font-size: 11px; color: var(--text-tertiary);
                text-transform: uppercase; letter-spacing: 0.1em; font-weight: 600; }
.reco-value { font-family: var(--font-mono); font-size: 28px; font-weight: 700;
              color: var(--text-primary); line-height: 1.1; margin: 4px 0; }
.reco-label { font-size: 11px; color: var(--text-secondary); }
.reco-card.conservative .reco-percent { color: var(--success); }
.reco-card.aggressive .reco-percent { color: var(--warning); }
.insights { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px;
            margin-bottom: 32px; }
.insight-card { background: var(--bg-card); border: 1px solid var(--border);
                border-radius: 10px; padding: 16px; transition: border-color 0.2s; }
.insight-card:hover { border-color: var(--border-bright); }
.insight-icon { display: inline-flex; align-items: center; justify-content: center;
                width: 28px; height: 28px; border-radius: 6px; margin-bottom: 10px;
                font-size: 14px; }
.insight-card.success .insight-icon { background: var(--success-bg); color: var(--success); }
.insight-card.warning .insight-icon { background: var(--warning-bg); color: var(--warning); }
.insight-card.info .insight-icon { background: var(--info-bg); color: var(--info); }
.insight-card.danger .insight-icon { background: var(--danger-bg); color: var(--danger); }
.insight-title { font-size: 11px; text-transform: uppercase; letter-spacing: 0.1em;
                 color: var(--text-tertiary); margin-bottom: 4px; font-weight: 600; }
.insight-value { font-family: var(--font-mono); font-size: 20px; font-weight: 700;
                 color: var(--text-primary); line-height: 1.2; margin-bottom: 4px; }
.insight-detail { font-size: 12px; color: var(--text-secondary); }
.section { margin-bottom: 32px; }
.section-header { display: flex; align-items: baseline; justify-content: space-between;
                  margin-bottom: 16px; border-bottom: 1px solid var(--border);
                  padding-bottom: 8px; }
.section-title { font-size: 18px; font-weight: 700; color: var(--text-primary);
                 letter-spacing: -0.01em; }
.section-meta { font-size: 12px; color: var(--text-tertiary); font-family: var(--font-mono); }
.tabs { display: flex; gap: 4px; margin-bottom: 16px; border-bottom: 1px solid var(--border); }
.tab { padding: 8px 16px; background: transparent; border: none;
       color: var(--text-secondary); font-family: inherit; font-size: 13px;
       font-weight: 600; cursor: pointer; border-bottom: 2px solid transparent;
       margin-bottom: -1px; transition: all 0.2s; }
.tab:hover { color: var(--text-primary); }
.tab.active { color: var(--accent); border-bottom-color: var(--accent); }
.tab-panel { display: none; }
.tab-panel.active { display: block; }
.chart-card { background: var(--bg-card); border: 1px solid var(--border);
              border-radius: 12px; padding: 24px; margin-bottom: 16px; }
.chart-explainer { font-size: 13px; color: var(--text-secondary);
                   margin-bottom: 20px; line-height: 1.6;
                   padding-left: 12px; border-left: 3px solid var(--accent-dim); }
.chart-explainer strong { color: var(--text-primary); }
canvas { max-height: 400px; }
.timeline { background: var(--bg-card); border: 1px solid var(--border);
            border-radius: 12px; padding: 24px; }
.timeline-item { display: grid; grid-template-columns: 100px 1fr; gap: 16px;
                 padding: 14px 0; border-bottom: 1px solid var(--border); }
.timeline-item:last-child { border-bottom: none; }
.timeline-phase { font-family: var(--font-mono); font-size: 11px; text-transform: uppercase;
                  letter-spacing: 0.1em; color: var(--text-tertiary); font-weight: 600;
                  padding-top: 2px; }
.timeline-phase.coarse { color: var(--info); }
.timeline-phase.bisect { color: var(--warning); }
.timeline-phase.verify { color: var(--success); }
.timeline-phase.trigger { color: var(--danger); }
.timeline-content { font-size: 13px; }
.timeline-flow { font-family: var(--font-mono); font-weight: 700;
                 color: var(--text-primary); margin-bottom: 4px; }
.timeline-detail { color: var(--text-secondary); }
details { background: var(--bg-secondary); border: 1px solid var(--border);
          border-radius: 10px; margin-bottom: 8px; }
details summary { padding: 14px 20px; cursor: pointer; font-weight: 600;
                  color: var(--text-primary); font-size: 14px; display: flex;
                  align-items: center; justify-content: space-between;
                  list-style: none; user-select: none; }
details summary::-webkit-details-marker { display: none; }
details summary::after { content: '↓'; font-family: var(--font-mono);
                         color: var(--text-tertiary); transition: transform 0.2s;
                         font-size: 14px; }
details[open] summary::after { transform: rotate(180deg); color: var(--accent); }
details > div { padding: 0 20px 20px; color: var(--text-secondary);
                font-size: 13px; line-height: 1.7; }
details table { font-size: 12px; }
table { width: 100%%; border-collapse: collapse; font-family: var(--font-mono);
        font-size: 12px; }
table th { text-align: left; padding: 10px 8px; border-bottom: 1px solid var(--border-bright);
           color: var(--text-tertiary); font-weight: 600; text-transform: uppercase;
           letter-spacing: 0.05em; font-size: 11px; background: var(--bg-elevated);
           position: sticky; top: 0; }
table td { padding: 8px; border-bottom: 1px solid var(--border); color: var(--text-secondary); }
table tr:hover td { background: var(--bg-card-hover); color: var(--text-primary); }
.row-coarse td:first-child { color: var(--info); font-weight: 600; }
.row-bisect td:first-child { color: var(--warning); font-weight: 600; }
.row-verify td:first-child { color: var(--success); font-weight: 600; }
footer { border-top: 1px solid var(--border); padding: 24px 0; margin-top: 48px;
         color: var(--text-tertiary); font-size: 12px; }
footer .container { display: flex; justify-content: space-between;
                    align-items: center; flex-wrap: wrap; gap: 16px; }
footer a { color: var(--accent); text-decoration: none; }
footer a:hover { text-decoration: underline; }
.footer-credits { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
.footer-credits .author { color: var(--text-secondary); font-weight: 600; }
@media (max-width: 900px) {
    .hero { padding: 24px; }
    .hero-grid { grid-template-columns: 1fr; gap: 24px; }
    .hero-value { font-size: 64px; }
    .insights { grid-template-columns: repeat(2, 1fr); }
    header .container { flex-direction: column; align-items: flex-start; }
    .header-meta { flex-wrap: wrap; }
}
@media (max-width: 600px) {
    .container { padding: 0 16px; }
    .insights { grid-template-columns: 1fr; }
    .timeline-item { grid-template-columns: 1fr; gap: 4px; }
    .tabs { overflow-x: auto; }
}
</style>
</head>
<body>

<header>
  <div class="container">
    <div class="brand">
      <div class="brand-logo">TMC</div>
      <div class="brand-text">
        <div class="brand-name">TMC Flow Test</div>
        <div class="brand-subtitle">Adaptive max-volumetric-flow detection</div>
      </div>
    </div>
    <div class="header-meta">
      <div class="header-meta-item"><span class="dot"></span><span>v%(version)s</span></div>
      <div class="header-meta-item"><span>%(timestamp)s</span></div>
      <div class="header-meta-item"><span>%(driver_label)s</span></div>
    </div>
  </div>
</header>

<div class="container">

%(hero_html)s

%(insights_html)s

<div class="section">
  <div class="section-header">
    <div class="section-title">Visual Analysis</div>
    <div class="section-meta">%(measurement_count)s measurements</div>
  </div>
  <div class="tabs">
    <button class="tab active" data-target="sg" onclick="showTab(event, 'sg')">StallGuard signal</button>
    <button class="tab" data-target="thermal" onclick="showTab(event, 'thermal')">Thermal profile</button>
    <button class="tab" data-target="variance" onclick="showTab(event, 'variance')">Run-to-run variance</button>
  </div>
  <div id="tab-sg" class="tab-panel active">
    <div class="chart-card">
      <div class="chart-explainer">
        The <strong>median SG line</strong> shows motor load at each tested
        flow. The <strong>shaded band</strong> covers the P25–P75 sample range.
        <strong>Lower line = more load.</strong> Sudden jumps, spikes upward,
        or wide bands indicate slip is starting. Phase boundaries marked as
        vertical lines.
      </div>
      <canvas id="sgChart"></canvas>
    </div>
  </div>
  <div id="tab-thermal" class="tab-panel">
    <div class="chart-card" id="thermalChartCard" style="display:none;">
      <div class="chart-explainer">
        Tracks <strong>hotend temperature</strong> (red),
        <strong>heater PWM</strong> (orange) and <strong>residence time</strong>
        in melt zone (green) per step. <strong>Two thermal limits to watch:</strong>
        <em>heater power</em> (PWM saturates at 1.0 + temperature falls)
        or <em>filament speed</em> (residence below your hotend's class minimum).
        Reference lines: V6 ≥1.5 s, Volcano ≥0.6 s, CHT/HF ≥0.3 s.
      </div>
      <canvas id="thermalChart"></canvas>
    </div>
    <div class="chart-card" id="thermalChartEmpty">
      <p style="color: var(--text-tertiary); text-align: center; padding: 40px;">
        No thermal data captured for this run.
      </p>
    </div>
  </div>
  <div id="tab-variance" class="tab-panel">
    <div class="chart-card">
      <div class="chart-explainer">
        <strong>Run-to-run CV</strong> measures how much the repeated runs at
        each flow varied from each other. Low CV (&lt;3 %%) means measurements
        are repeatable. When CV spikes upward, the motor is producing
        inconsistent SG readings between runs — the classic slip signature.
        Bars are coloured: green (clean), yellow (borderline), red (above CV
        trigger threshold).
      </div>
      <canvas id="cvChart"></canvas>
    </div>
  </div>
</div>

%(timeline_html)s

<div class="section">
  <div class="section-header">
    <div class="section-title">Test Details</div>
    <div class="section-meta">click to expand</div>
  </div>
  <details>
    <summary>Full data table</summary>
    <div>%(data_table)s</div>
  </details>
  <details>
    <summary>Test configuration</summary>
    <div>%(meta_html)s</div>
  </details>
  <details>
    <summary>TMC driver settings at test start</summary>
    <div>%(tmc_settings_html)s</div>
  </details>
  %(decision_trail_html)s
</div>

<div class="section">
  <div class="section-header">
    <div class="section-title">Reference</div>
    <div class="section-meta">explanations</div>
  </div>

  <details>
    <summary>What does "max volumetric flow" actually mean?</summary>
    <div><p>It's the maximum amount of plastic your extruder can push per
    second while still maintaining grip on the filament. Above this value
    the motor either slips on the gear teeth or extrudes filament that
    wasn't fully melted. Both lead to under-extrusion in real prints.
    Slicers use this value to cap volumetric speed even when you set high
    print speeds.</p></div>
  </details>

  <details>
    <summary>StallGuard, SG_RESULT, CV — what's measured?</summary>
    <div><p><strong>StallGuard</strong> is a TMC driver feature that
    measures motor torque without external sensors. It works by analyzing
    the back-EMF of the coils. <strong>SG_RESULT</strong> is its output —
    on TMC5160 it ranges 0–1023 (lower = more load), on TMC2209 it's
    0–510. The plugin samples this value many times per second.</p>
    <p><strong>Run-to-run CV</strong> is the coefficient of variation
    between repeated measurements at the same flow. Low CV (&lt;3 %%)
    means measurements are repeatable. High CV means something is
    inconsistent — usually slip starting.</p></div>
  </details>

  <details>
    <summary>Why the 80%% / 90%% recommendations?</summary>
    <div><p>The "Maximum Safe" value is what the plugin found just below
    where slip starts. The 80%% recommendation gives you a 20%% safety
    buffer — recommended for everyday use because filament thickness
    varies, hotend temperature drifts, and longer prints stress the system
    more. The 90%% value is closer to the limit and should only be used
    after you've validated it with several long real-world prints.</p></div>
  </details>

  <details>
    <summary>Hotend temperature & heater PWM</summary>
    <div><p>The thermal chart logs the hotend's behavior during each step.
    <strong>Temperature drops</strong> at high flow happen when the heater
    can't melt filament fast enough. <strong>Heater PWM</strong> reaching
    1.0 means Klipper is asking for full power; if it stays there for
    several seconds AND temperature falls, the system is near thermal
    saturation. <strong>Driver thermal flags</strong> (OTPW ≥120 °C,
    OT ≥150 °C) appear as triangles when the TMC chip itself gets hot.
    None of this triggers the test or affects slip detection — purely
    informational.</p></div>
  </details>

  <details>
    <summary>Residence time in the melt zone</summary>
    <div><p>The number of seconds each piece of filament spends inside
    the heated melt zone. Computed as
    <code>melt_zone_length / linear_feed_speed</code>. As flow increases,
    residence time shrinks — even with plenty of heater power, very short
    residence times mean the filament core stays cold.</p>
    <p>Each hotend design has a <strong>minimum residence time</strong>
    needed for clean melting. The reference lines in the chart show these
    minimums, ordered from weakest to strongest:</p>
    <ul>
    <li><strong>V6-class</strong> — minimum ~1.5 s residence</li>
    <li><strong>Volcano-class</strong> — minimum ~0.6 s residence</li>
    <li><strong>CHT / HF-class</strong> — minimum ~0.3 s residence</li>
    <li><strong>Goliath / Bondtech-CHT</strong> — &lt; 0.3 s ok</li>
    </ul>
    <p>The tooltip shows the <strong>minimum required hotend class</strong>
    at each flow — anything stronger also works. Example: 0.8 s residence
    needs at minimum a Volcano, but a CHT or Goliath would also handle it
    fine. If your hotend is below the minimum class shown at the slip point,
    the bottleneck was filament speed (not heater wattage).</p></div>
  </details>

  <details>
    <summary>Coloured zones &amp; thermal stress score</summary>
    <div><p>The chart backgrounds are tinted in three zones based on a
    heuristic <strong>thermal stress score</strong> calculated per step.
    The score combines five signals (each weighted differently):</p>
    <ul>
    <li><strong>Heater PWM level</strong> — how hard the heater is working
    (0–30 points). PWM 30%% adds nothing, 95%%+ adds full 30.</li>
    <li><strong>Temperature drop from target</strong> — how much actual
    temp falls below target (0–25 points). 0 °C drops adds nothing,
    5 °C+ adds full 25.</li>
    <li><strong>PWM rising trend</strong> — heater taking on more work
    relative to early-test baseline (0–15 points).</li>
    <li><strong>PWM peak hits</strong> — saturation events even briefly
    (0–10 points).</li>
    <li><strong>Intra-run SG drift</strong> — load drifting upward
    DURING individual 5-second runs (0–20 points). This is a strong
    cold-extrusion signature: as filament heats up too slowly, motor
    load increases continuously through a single run rather than
    staying steady. Pure motor slip jumps abruptly; cold extrusion
    drifts gradually.</li>
    </ul>
    <p>Score interpretation:
    <strong style="color:#3fb950">0–30 = green / stable</strong>,
    <strong style="color:#d29922">30–60 = yellow / moderate stress</strong>,
    <strong style="color:#f85149">60–100 = red / high stress (cold extrusion likely)</strong>.</p>
    <p>The yellow zone in the charts marks where the score first crosses
    30. <strong>Important:</strong> This is a heuristic combining heater
    behavior with within-run load trends. It does NOT trigger or modify
    the slip-detection logic. Hover any thermal-chart point to see the
    per-step score AND the intra-run drift value (positive %% = load
    increased during the run, the cold-extrusion fingerprint).</p>
    <p><strong>About the data:</strong> All thermal values (PWM, temperature)
    are sampled at 4 Hz <em>during active extrusion only</em> — never
    during the cooldown gaps between reps. This avoids the otherwise
    confusing pattern where PWM drops to baseline and temperature
    overshoots while the heater is idle.</p></div>
  </details>
</div>

</div>

<footer>
  <div class="container">
    <div class="footer-credits">
      <span>Plugin by</span>
      <span class="author">Steven (Fragmon) — Crydteam</span>
      <span>·</span>
      <a href="https://www.youtube.com/@crydteamprinting">YouTube</a>
      <span>·</span>
      <a href="https://github.com/Fragmon/klipper_max_flow_test">GitHub</a>
    </div>
    <div>Generated %(timestamp)s · v%(version)s · GPL-3.0</div>
  </div>
</footer>

<script>
function showTab(event, name) {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    event.currentTarget.classList.add('active');
    document.getElementById('tab-' + name).classList.add('active');
}

const flows = %(flows)s;
const phases = %(phases)s;
const sgMedian = %(sg_median)s;
const sgP25 = %(sg_p25)s;
const sgP75 = %(sg_p75)s;
const sgAvg = %(sg_avg)s;
const cvData = %(cv_data)s;
const tempActual = %(temp_actual)s;
const tempTarget = %(temp_target)s;
const tempMin = %(temp_min)s;
const tempDrop = %(temp_drop)s;
const pwmAvg = %(pwm_avg)s;
const pwmMax = %(pwm_max)s;
const tmcOtpw = %(tmc_otpw)s;
const tmcOt = %(tmc_ot)s;
const linearSpeeds = %(linear_speeds)s;
const residenceTimes = %(residence_times)s;
const chartEvents = %(chart_events)s;
const triggerFlow = %(trigger_flow)s;
const verifyFlow = %(verify_flow)s;
const cvTriggerThreshold = %(cv_trigger)s;
const coldExtrusionHint = %(cold_extrusion_hint)s;
const coldOnsetFlow = %(cold_onset_flow)s;
const stressScores = %(stress_scores)s;
const intraDrift = %(intra_drift)s;

Chart.defaults.color = '#8b949e';
Chart.defaults.borderColor = '#30363d';
Chart.defaults.font.family = "'Manrope', system-ui, sans-serif";

// Build phase-boundary annotations for SG chart
const phaseAnnotations = {};
let lastPhase = null;
for (let i = 0; i < phases.length; i++) {
    if (phases[i] !== lastPhase && lastPhase !== null) {
        phaseAnnotations['phase_' + i] = {
            type: 'line',
            xMin: flows[i] - 0.5, xMax: flows[i] - 0.5,
            borderColor: 'rgba(110, 118, 129, 0.4)',
            borderWidth: 1, borderDash: [2, 4],
        };
    }
    lastPhase = phases[i];
}

// Build trigger event annotations
const eventAnnotations = {};
chartEvents.forEach((ev, i) => {
    eventAnnotations['ev_' + i] = {
        type: 'point',
        xValue: ev.flow,
        yValue: ev.value,
        backgroundColor: 'rgba(248, 81, 73, 0.85)',
        borderColor: '#f85149',
        radius: 6, borderWidth: 2,
    };
});

if (triggerFlow !== null) {
    eventAnnotations['triggerLine'] = {
        type: 'line',
        xMin: triggerFlow, xMax: triggerFlow,
        borderColor: '#f85149', borderWidth: 2, borderDash: [4,4],
        label: { display: true, content: 'first trigger',
                 position: 'start', backgroundColor: '#f85149',
                 color: 'white', font: {size: 10}, padding: 4 },
    };
}
if (verifyFlow !== null) {
    eventAnnotations['verifyLine'] = {
        type: 'line',
        xMin: verifyFlow, xMax: verifyFlow,
        borderColor: '#3fb950', borderWidth: 2,
        label: { display: true, content: 'verified',
                 position: 'end', backgroundColor: '#3fb950',
                 color: 'white', font: {size: 10}, padding: 4 },
    };
}
// Note: the cold-extrusion hint line is now drawn by buildZoneAnnotations()
// as part of the colored-zone system — that way the hint and the
// auto-detection share the same visual marker.

const sgAnnotations = Object.assign({}, phaseAnnotations, eventAnnotations);

// ─── Color zones (visual hint, no test impact) ───────────────────
// Three coloured background bands divide the flow range:
//   green:  start → cold-extrusion onset (or slip if no cold)
//   yellow: cold-extrusion onset → slip trigger
//   red:    slip trigger → max
// Each chart that wants the zones merges these annotations into its
// own annotations object.
function buildZoneAnnotations() {
    const z = {};
    // Determine zone boundaries based on which detections fired.
    const flowMin = %(flow_min_for_zones)s;
    const flowMax = %(flow_max_for_zones)s;
    // The user-supplied COLD_EXTRUSION_HINT takes precedence over the
    // auto-detection — visual observation of curling/sputtering is more
    // reliable than the heater-PWM heuristic for many setups (especially
    // strong heaters on weaker hotends, where curling happens long
    // before any heater-side stress is visible).
    const coldFlow = (coldExtrusionHint > 0) ? coldExtrusionHint
                                              : coldOnsetFlow;
    const slipFlow = triggerFlow;

    // Three states are possible:
    //  1. cold + slip: green / yellow / red
    //  2. only slip:   green / red (no yellow zone)
    //  3. only cold:   green / yellow (no red zone)
    //  4. neither:     all green
    let greenEnd, yellowEnd;
    if (coldFlow !== null && slipFlow !== null) {
        if (coldFlow < slipFlow) {
            greenEnd = coldFlow;
            yellowEnd = slipFlow;
        } else {
            // Cold detected after slip — unusual, treat as just slip
            greenEnd = slipFlow;
            yellowEnd = null;
        }
    } else if (slipFlow !== null) {
        greenEnd = slipFlow;
        yellowEnd = null;
    } else if (coldFlow !== null) {
        greenEnd = coldFlow;
        yellowEnd = flowMax;
    } else {
        greenEnd = flowMax;
        yellowEnd = null;
    }

    // Green zone (safe)
    z['zoneGreen'] = {
        type: 'box',
        xMin: flowMin, xMax: greenEnd,
        backgroundColor: 'rgba(63, 185, 80, 0.06)',
        borderWidth: 0,
        drawTime: 'beforeDatasetsDraw',
    };
    // Yellow zone (cold-extrusion suspected)
    if (yellowEnd !== null && yellowEnd > greenEnd) {
        z['zoneYellow'] = {
            type: 'box',
            xMin: greenEnd, xMax: yellowEnd,
            backgroundColor: 'rgba(210, 153, 34, 0.10)',
            borderWidth: 0,
            drawTime: 'beforeDatasetsDraw',
        };
    }
    // Red zone (slip / stall)
    const redStart = (yellowEnd !== null) ? yellowEnd : greenEnd;
    if (redStart < flowMax && slipFlow !== null) {
        z['zoneRed'] = {
            type: 'box',
            xMin: redStart, xMax: flowMax,
            backgroundColor: 'rgba(248, 81, 73, 0.08)',
            borderWidth: 0,
            drawTime: 'beforeDatasetsDraw',
        };
    }
    // Cold-onset marker line. Label depends on source:
    //   - if the user supplied COLD_EXTRUSION_HINT, it's based on
    //     visual observation
    //   - otherwise auto-detection from heater-PWM saturation
    if (coldFlow !== null) {
        const labelText = (coldExtrusionHint > 0)
            ? '❄ cold-ext. (user)'
            : '❄ cold-ext. (auto)';
        z['coldOnsetLine'] = {
            type: 'line',
            xMin: coldFlow, xMax: coldFlow,
            borderColor: 'rgba(210, 153, 34, 0.7)',
            borderWidth: 2, borderDash: [5, 3],
            label: { display: true,
                     content: labelText,
                     position: 'start',
                     backgroundColor: 'rgba(210, 153, 34, 0.85)',
                     color: 'white', font: {size: 10}, padding: 3 },
        };
    }
    return z;
}

const zoneAnnotations = buildZoneAnnotations();
Object.assign(sgAnnotations, zoneAnnotations);

// SG chart
new Chart(document.getElementById('sgChart'), {
    type: 'line',
    data: { labels: flows, datasets: [
        { label: 'P75', data: sgP75,
          borderColor: 'rgba(88, 166, 255, 0.4)',
          backgroundColor: 'rgba(88, 166, 255, 0.08)',
          fill: '+1', borderDash: [3,3], pointRadius: 2 },
        { label: 'median', data: sgMedian,
          borderColor: '#58a6ff', borderWidth: 3, pointRadius: 5, fill: false },
        { label: 'P25', data: sgP25,
          borderColor: 'rgba(88, 166, 255, 0.4)',
          fill: false, borderDash: [3,3], pointRadius: 2 },
        { label: 'avg', data: sgAvg,
          borderColor: 'rgba(139, 148, 158, 0.6)',
          borderDash: [6,3], fill: false, borderWidth: 1, pointRadius: 0 },
    ]},
    options: {
        responsive: true, interaction: { mode: 'index', intersect: false },
        scales: {
            x: { title: { display: true, text: 'Flow Rate (mm³/s)' } },
            y: { title: { display: true, text: '%(sg_label)s' } },
        },
        plugins: {
            legend: { position: 'top' },
            annotation: { annotations: sgAnnotations },
        },
    },
});

// Thermal chart — only if any thermal data exists
const hasThermalData = tempActual.some(v => v !== null);
if (hasThermalData) {
    document.getElementById('thermalChartCard').style.display = 'block';
    document.getElementById('thermalChartEmpty').style.display = 'none';

    const otpwPoints = [];
    const otPoints = [];
    for (let i = 0; i < flows.length; i++) {
        if (tmcOtpw[i]) otpwPoints.push({x: flows[i], y: 0.05});
        if (tmcOt[i])   otPoints.push({x: flows[i], y: 0.05});
    }

    const validTemps = tempActual.concat(tempMin)
        .filter(v => v !== null && v !== undefined);
    const validTargets = tempTarget.filter(v => v !== null && v !== undefined);
    let tempYMin = null, tempYMax = null;
    if (validTemps.length > 0) {
        tempYMin = Math.min(...validTemps) - 2;
        tempYMax = Math.max(...validTemps, ...validTargets) + 2;
    }

    new Chart(document.getElementById('thermalChart'), {
        type: 'line',
        data: { labels: flows, datasets: [
            { label: 'temp target', data: tempTarget,
              borderColor: 'rgba(88, 166, 255, 0.6)',
              borderDash: [4, 3], borderWidth: 1.5,
              fill: false, pointRadius: 0, yAxisID: 'yTemp' },
            { label: 'temp actual', data: tempActual,
              borderColor: '#f85149', borderWidth: 2.5, pointRadius: 4,
              fill: false, yAxisID: 'yTemp' },
            { label: 'temp min', data: tempMin,
              borderColor: 'rgba(255, 152, 0, 0.6)',
              borderDash: [2, 4], borderWidth: 1, fill: false,
              pointRadius: 2, yAxisID: 'yTemp' },
            { label: 'heater PWM (avg)', data: pwmAvg,
              borderColor: '#d29922',
              backgroundColor: 'rgba(210, 153, 34, 0.18)',
              fill: 'origin', borderWidth: 2, pointRadius: 3, yAxisID: 'yPwm' },
            { label: 'heater PWM (max)', data: pwmMax,
              borderColor: 'rgba(230, 81, 0, 0.7)',
              borderDash: [3, 3], borderWidth: 1, pointRadius: 0,
              fill: false, yAxisID: 'yPwm' },
            { label: 'residence (s)', data: residenceTimes,
              borderColor: '#3fb950', borderWidth: 2.5, pointRadius: 4,
              fill: false, yAxisID: 'yResidence' },
            { label: 'driver OTPW (≥120 °C)', data: otpwPoints,
              showLine: false, pointStyle: 'triangle', pointRadius: 8,
              backgroundColor: 'rgba(255, 152, 0, 0.9)',
              borderColor: '#e65100', yAxisID: 'yPwm' },
            { label: 'driver OT (≥150 °C)', data: otPoints,
              showLine: false, pointStyle: 'triangle', pointRadius: 10,
              backgroundColor: 'rgba(244, 67, 54, 0.9)',
              borderColor: '#b71c1c', yAxisID: 'yPwm' },
        ]},
        options: {
            responsive: true, interaction: { mode: 'index', intersect: false },
            scales: {
                x: { title: { display: true, text: 'Flow Rate (mm³/s)' } },
                yTemp: {
                    type: 'linear', position: 'left',
                    title: { display: true, text: 'Temperature (°C)' },
                    min: tempYMin, max: tempYMax,
                },
                yPwm: {
                    type: 'linear', position: 'right',
                    title: { display: true, text: 'PWM (0-1)' },
                    min: 0, max: 1.0,
                    grid: { drawOnChartArea: false },
                },
                yResidence: {
                    type: 'linear', position: 'right',
                    title: { display: true, text: 'Residence (s)' },
                    min: 0, grid: { drawOnChartArea: false },
                    afterFit: function(s) { s.width = 50; },
                },
            },
            plugins: {
                legend: { position: 'top' },
                tooltip: {
                    callbacks: {
                        afterBody: function(items) {
                            const idx = items[0].dataIndex;
                            const lines = [];
                            const drop = tempDrop[idx];
                            if (drop !== null && drop !== undefined && drop > 0.1) {
                                lines.push('Drop from target: ' + drop.toFixed(1) + ' °C');
                            }
                            const ls = linearSpeeds[idx];
                            const rt = residenceTimes[idx];
                            if (ls !== null && ls !== undefined) {
                                lines.push('Linear feed: ' + ls.toFixed(1) + ' mm/s');
                            }
                            if (rt !== null && rt !== undefined) {
                                let cls = '';
                                // Minimum hotend class needed to maintain
                                // this residence time — anything stronger
                                // (e.g. CHT > Volcano > V6) also works.
                                if (rt >= 1.5) cls = 'V6 or better';
                                else if (rt >= 0.6) cls = 'Volcano or better (CHT/Goliath OK)';
                                else if (rt >= 0.3) cls = 'CHT/HF or better';
                                else cls = 'Goliath / Bondtech-CHT only';
                                lines.push('Min. hotend: ' + cls);
                            }
                            // Thermal stress score (heuristic 0-100)
                            const ss = stressScores[idx];
                            if (ss !== null && ss !== undefined) {
                                let stressLabel = '';
                                if (ss < 30) stressLabel = 'low';
                                else if (ss < 60) stressLabel = 'moderate';
                                else stressLabel = 'high';
                                lines.push('Thermal stress: ' + ss.toFixed(0) +
                                           '/100 (' + stressLabel + ')');
                            }
                            // Intra-run SG drift (cold extrusion fingerprint)
                            const drift = intraDrift[idx];
                            if (drift !== null && drift !== undefined) {
                                let driftLabel = '';
                                if (Math.abs(drift) < 0.5) driftLabel = 'stable';
                                else if (drift > 3) driftLabel = 'load growing — cold suspected';
                                else if (drift > 1) driftLabel = 'load growing slightly';
                                else if (drift < -1) driftLabel = 'load decreasing';
                                else driftLabel = 'minimal change';
                                lines.push('Intra-run drift: ' +
                                           (drift > 0 ? '+' : '') +
                                           drift.toFixed(1) + '%% (' +
                                           driftLabel + ')');
                            }
                            return lines;
                        }
                    }
                },
                annotation: {
                    annotations: Object.assign({
                        v6Residence: {
                            type: 'line', yMin: 1.5, yMax: 1.5, yScaleID: 'yResidence',
                            borderColor: 'rgba(63, 185, 80, 0.4)', borderDash: [4, 4],
                            label: { display: true, content: 'V6 min 1.5s', position: 'start',
                                     backgroundColor: 'rgba(63, 185, 80, 0.7)',
                                     color: 'white', font: {size: 9}, padding: 2 },
                        },
                        volcanoResidence: {
                            type: 'line', yMin: 0.6, yMax: 0.6, yScaleID: 'yResidence',
                            borderColor: 'rgba(210, 153, 34, 0.4)', borderDash: [4, 4],
                            label: { display: true, content: 'Volcano min 0.6s', position: 'start',
                                     backgroundColor: 'rgba(210, 153, 34, 0.7)',
                                     color: 'white', font: {size: 9}, padding: 2 },
                        },
                        chtResidence: {
                            type: 'line', yMin: 0.3, yMax: 0.3, yScaleID: 'yResidence',
                            borderColor: 'rgba(248, 81, 73, 0.4)', borderDash: [4, 4],
                            label: { display: true, content: 'CHT min 0.3s', position: 'start',
                                     backgroundColor: 'rgba(248, 81, 73, 0.7)',
                                     color: 'white', font: {size: 9}, padding: 2 },
                        },
                    }, zoneAnnotations),
                },
            },
        },
    });
}

// CV chart — bars colored by severity
new Chart(document.getElementById('cvChart'), {
    type: 'bar',
    data: { labels: flows, datasets: [
        { label: 'run-to-run CV (%%)', data: cvData,
          backgroundColor: cvData.map(v =>
              v === null ? '#6e7681'
              : v >= cvTriggerThreshold ? '#f85149'
              : v >= cvTriggerThreshold * 0.6 ? '#d29922'
              : '#3fb950'),
          borderWidth: 0 },
    ]},
    options: {
        responsive: true,
        scales: {
            x: { title: { display: true, text: 'Flow Rate (mm³/s)' } },
            y: { title: { display: true, text: 'CV (%%)' }, min: 0 },
        },
        plugins: {
            legend: { display: false },
            annotation: {
                annotations: Object.assign({
                    cvTrigger: {
                        type: 'line', yMin: cvTriggerThreshold, yMax: cvTriggerThreshold,
                        borderColor: 'rgba(248, 81, 73, 0.5)', borderDash: [4, 4],
                        label: { display: true,
                                 content: 'CV trigger ' + cvTriggerThreshold + '%%',
                                 position: 'end', backgroundColor: '#f85149',
                                 color: 'white', font: {size: 10}, padding: 3 },
                    },
                }, zoneAnnotations),
            },
        },
    },
});
</script>
</body>
</html>"""
        # Build trigger-event annotations for the chart. We attach
        # each event to the LAST results-array index whose flow matches
        # (since re-tests replace earlier entries, this is well-defined
        # except for verify-fail where the failed verify still sits in
        # results — in that case we attach to the verify entry directly).
        chart_events = []
        if final_result is not None:
            for ev in (final_result.get('trigger_events') or []):
                # Find the matching results index (search in reverse so
                # we hit the last/most-recent entry for this flow+phase).
                idx = None
                for i in range(len(results) - 1, -1, -1):
                    rr = results[i]
                    if (rr.get('phase') == ev['phase']
                            and abs(rr.get('flow', 0) - ev['flow'])
                            < 0.01):
                        idx = i
                        break
                if idx is None:
                    continue
                chart_events.append({
                    'idx': idx,
                    'flow': ev['flow'],
                    'phase': ev['phase'],
                    'kind': ev['kind'],
                    'reason': ev['reason'],
                    'cv': ev.get('cv'),
                    'iqr': ev.get('iqr'),
                })

        # Healthy-range bands for the chart (drawn as horizontal
        # background regions if baseline_stats is available).
        healthy_iqr = None
        if final_result is not None:
            bs = final_result.get('baseline_stats')
            if bs and 'iqr_min' in bs and 'iqr_max' in bs:
                healthy_iqr = {
                    'min': bs['iqr_min'],
                    'max': max(bs['iqr_max'],
                               bs['iqr_median'] * 1.5),
                    'median': bs['iqr_median'],
                }

        # First trigger flow + verify flow for chart annotations
        first_trigger_flow = None
        for ev in (trigger_events or []):
            if ev.get('kind') == 'trigger':
                first_trigger_flow = ev['flow']
                break
        verify_flow = None
        if max_safe is not None:
            verify_flow = max_safe

        # Cold-extrusion onset detection (purely visual, no test impact).
        # Returns the flow at which heater saturation + temperature drop
        # were first observed together. May be None.
        cold_onset_flow = self._detect_cold_extrusion_onset(results)

        # Continuous thermal stress score per step (0-100). Used to colour
        # the charts and to drive the cold-extrusion onset detection above.
        # See _compute_thermal_stress_per_step for the scoring breakdown.
        stress_scores_pairs = self._compute_thermal_stress_per_step(results)
        stress_scores = [s for _, s in stress_scores_pairs]
        max_stress = max((s for s in stress_scores if s is not None),
                         default=None)
        max_stress_flow = None
        if max_stress is not None:
            for flow, score in stress_scores_pairs:
                if score == max_stress:
                    max_stress_flow = flow
                    break

        # The chart's coloured background zones use these boundaries.
        # min_flow / max_flow span the X-axis of the data we have.
        if results:
            min_flow_for_zones = min(r['flow'] for r in results)
            max_flow_for_zones = max(r['flow'] for r in results)
        else:
            min_flow_for_zones = 0
            max_flow_for_zones = 100

        rendered = html % {
            'timestamp': meta.get('timestamp', '-'),
            'version': MODULE_VERSION,
            'driver_label': driver_label_full,
            'measurement_count': len(results),
            'meta_html': meta_html,
            'tmc_settings_html': tmc_settings_html,
            'decision_trail_html': decision_trail_html,
            'hero_html': hero_html,
            'insights_html': insights_html,
            'timeline_html': timeline_html,
            'sg_label': sg_label,
            'data_table': table,
            'flows': json.dumps(flows),
            'phases': json.dumps(phases),
            'sg_median': json.dumps(sg_median),
            'sg_p25': json.dumps(sg_p25),
            'sg_p75': json.dumps(sg_p75),
            'sg_avg': json.dumps(sg_avg),
            'cv_data': json.dumps(cv_data),
            'cv_trigger': json.dumps(cv_trigger_threshold),
            'chart_events': json.dumps(chart_events),
            'trigger_flow': json.dumps(first_trigger_flow),
            'verify_flow': json.dumps(verify_flow),
            'cold_onset_flow': json.dumps(cold_onset_flow),
            'stress_scores': json.dumps(stress_scores),
            'flow_min_for_zones': json.dumps(min_flow_for_zones),
            'flow_max_for_zones': json.dumps(max_flow_for_zones),
            'cold_extrusion_hint': json.dumps(
                meta.get('cold_extrusion_hint', 0)),
            # Thermal data — see _aggregate_thermal_samples
            'temp_actual': json.dumps(temp_actual),
            'temp_target': json.dumps(temp_target),
            'temp_min': json.dumps(temp_min),
            'temp_drop': json.dumps(temp_drop),
            'pwm_avg': json.dumps(pwm_avg),
            'pwm_max': json.dumps(pwm_max),
            'tmc_otpw': json.dumps(tmc_otpw),
            'tmc_ot': json.dumps(tmc_ot),
            'intra_drift': json.dumps(intra_drift),
            # Filament-speed / residence-time helpers
            'linear_speeds': json.dumps(linear_speeds),
            'residence_times': json.dumps(residence_times),
        }
        with open(path, 'w') as f:
            f.write(rendered)

    def _save_report(self, results, meta, timestamp, limit_reason,
                     no_html, gcmd=None, announce=True,
                     final_result=None):
        """Save CSV and HTML report. Safe to call repeatedly."""
        if not results:
            return
        try:
            os.makedirs(self.output_dir, exist_ok=True)
            csv_path = os.path.join(
                self.output_dir, 'tmc_flow_%s.csv' % timestamp)
            self._write_csv(csv_path, results, meta)
            if announce and gcmd is not None:
                gcmd.respond_info("CSV saved: %s" % csv_path)
            if not no_html:
                html_path = os.path.join(
                    self.output_dir,
                    'tmc_flow_%s.html' % timestamp)
                self._write_html(html_path, results, meta, limit_reason,
                                 final_result=final_result)
                if announce and gcmd is not None:
                    gcmd.respond_info("HTML saved: %s" % html_path)
        except Exception as e:
            if gcmd is not None:
                gcmd.respond_info(
                    "Warning: report write failed: %s" % e)
            logging.exception("tmc_flow_test: report write failed")

    # ─── Single flow step measurement ───────────────────────────────

    # ─── Thermal data collection ───────────────────────────────────
    # Read hotend temperature + heater PWM + TMC driver thermal flags.
    # These values are LOGGED ONLY (CSV columns) — no triggers, no
    # auto-detection, no behavioural change. They give the user
    # context to spot thermal issues during analysis (e.g. a slip
    # trigger that coincided with a temp drop is suspicious).

    def _get_thermal_snapshot(self):
        """Return a dict with current hotend + driver thermal state.
        All keys may be None if the relevant subsystem isn't available
        — treated as 'no data' downstream.
        """
        snap = {
            'temp_actual': None,    # current hotend temp (°C)
            'temp_target': None,    # target hotend temp (°C)
            'heater_pwm': None,     # current heater PWM, 0.0-1.0
            'tmc_otpw': None,       # TMC over-temperature warning flag (≥120 °C)
            'tmc_ot': None,         # TMC over-temperature error flag (≥150 °C)
        }

        # Hotend temp + PWM via Klipper extruder/heater objects
        try:
            extruder = self.printer.lookup_object('extruder')
            heater = extruder.get_heater()
            cur_t, target_t = heater.get_temp(self.reactor.monotonic())
            snap['temp_actual'] = float(cur_t)
            snap['temp_target'] = float(target_t)
            # last_pwm_value is always present; for both PID and MPC
            # control methods Klipper exposes the actual fired PWM
            try:
                pwm = heater.last_pwm_value
                if pwm is not None:
                    snap['heater_pwm'] = float(pwm)
            except AttributeError:
                pass
        except Exception:
            pass

        # TMC driver thermal warning flags via DRV_STATUS
        if self.tmc is not None:
            for field, key in (('otpw', 'tmc_otpw'), ('ot', 'tmc_ot')):
                try:
                    val = self.tmc.fields.get_field(field)
                    if val is not None:
                        snap[key] = int(val)
                except (KeyError, AttributeError):
                    pass

        return snap

    @staticmethod
    def _aggregate_thermal_samples(samples):
        """Reduce a list of per-rep thermal snapshots to summary stats.
        Each input is a dict from _get_thermal_snapshot. Returns aggregate
        across all reps with min/max/avg where meaningful.
        """
        agg = {
            'temp_start': None, 'temp_end': None,
            'temp_min': None, 'temp_max': None, 'temp_avg': None,
            'temp_target': None, 'temp_drop': None,
            'pwm_min': None, 'pwm_max': None, 'pwm_avg': None,
            'tmc_otpw_any': 0, 'tmc_ot_any': 0,
        }
        if not samples:
            return agg

        # Temperature aggregation
        temps = [s['temp_actual'] for s in samples
                 if s.get('temp_actual') is not None]
        if temps:
            agg['temp_start'] = temps[0]
            agg['temp_end'] = temps[-1]
            agg['temp_min'] = min(temps)
            agg['temp_max'] = max(temps)
            agg['temp_avg'] = sum(temps) / len(temps)

        targets = [s['temp_target'] for s in samples
                   if s.get('temp_target') is not None]
        if targets:
            agg['temp_target'] = targets[-1]  # last target wins
            if temps:
                agg['temp_drop'] = max(targets[-1] - min(temps), 0.0)

        # PWM aggregation
        pwms = [s['heater_pwm'] for s in samples
                if s.get('heater_pwm') is not None]
        if pwms:
            agg['pwm_min'] = min(pwms)
            agg['pwm_max'] = max(pwms)
            agg['pwm_avg'] = sum(pwms) / len(pwms)

        # Driver thermal warning flags — flagged if ANY sample saw it
        agg['tmc_otpw_any'] = int(any(
            s.get('tmc_otpw') for s in samples
            if s.get('tmc_otpw') is not None))
        agg['tmc_ot_any'] = int(any(
            s.get('tmc_ot') for s in samples
            if s.get('tmc_ot') is not None))

        return agg

    @staticmethod
    def _compute_thermal_stress_per_step(results):
        """Compute a 0-100 thermal stress score for each step.

        The score combines five signals to estimate how close the
        hotend system is to a cold-extrusion regime:

        - PWM absolute level (0-30 points): how hard the heater is working
        - Temperature drop (0-25 points): how much actual temp falls below target
        - PWM rising trend vs baseline (0-15 points): heater taking on more work
        - PWM peak proximity (0-10 points): hits at saturation
        - Intra-run SG drift (0-20 points): SG drifting load-ward DURING a run,
          a strong cold-extrusion fingerprint (see _compute_intra_run_trend)

        This is a HEURISTIC. It does not replace direct visual inspection
        of the strand at the nozzle, and it does not trigger anything in
        the test. Its only purpose is to colour-code the chart so the
        user gets a visual hint of WHERE in the flow range thermal stress
        starts to build.

        Returns a list of (flow, score) tuples — one per step, in
        results order. Score is None if no thermal data was available
        for that step.
        """
        # Build PWM baseline from the first 3 coarse steps
        baseline_pwms = []
        for r in results:
            if r.get('phase', 'coarse') != 'coarse':
                continue
            t = r.get('thermal') or {}
            p = t.get('pwm_avg')
            if p is not None:
                baseline_pwms.append(p)
                if len(baseline_pwms) >= 3:
                    break
        baseline = (sum(baseline_pwms) / len(baseline_pwms)
                    if baseline_pwms else None)

        scores = []
        for r in results:
            flow = r['flow']
            t = r.get('thermal') or {}
            ir = r.get('intra_run') or {}
            pwm_avg = t.get('pwm_avg')
            pwm_max = t.get('pwm_max')
            drop = t.get('temp_drop')
            mean_drift_load = ir.get('mean_drift_load') if ir else None
            consistent_growing = ir.get('consistent_growing_load') \
                if ir else 0

            # If we have NEITHER thermal nor intra-run, mark as no data
            if pwm_avg is None and drop is None and mean_drift_load is None:
                scores.append((flow, None))
                continue

            score = 0.0

            # 1) PWM absolute level (0-30)
            if pwm_avg is not None:
                # PWM=0.30 ≈ 0 stress, PWM=0.95 ≈ ~30
                score += min(30, max(0, (pwm_avg - 0.3) * 46))

            # 2) Temperature drop (0-25)
            if drop is not None:
                # 0 °C = 0, 5 °C = 25
                score += min(25, max(0, drop * 5))

            # 3) PWM rising trend (0-15)
            if pwm_avg is not None and baseline is not None:
                delta = pwm_avg - baseline
                # +0.20 PWM = +15 stress
                score += min(15, max(0, delta * 75))

            # 4) PWM peak saturation (0-10)
            if pwm_max is not None:
                if pwm_max >= 0.99:
                    score += 10
                elif pwm_max >= 0.85:
                    score += 5

            # 5) Intra-run SG drift toward higher load (0-20)
            # Positive drift_load = SG drifted toward higher-load
            # direction during the run = filament getting harder to push
            # = classic cold-extrusion fingerprint
            if mean_drift_load is not None:
                if mean_drift_load >= 5.0:
                    score += 20  # very strong gradient
                elif mean_drift_load >= 3.0:
                    score += 15
                elif mean_drift_load >= 1.5:
                    score += 10
                elif mean_drift_load >= 0.5:
                    score += 5
                # Bonus if multiple runs all show growing load
                # (consistency check — rules out a single fluke)
                if consistent_growing >= 3:
                    score += 5

            scores.append((flow, min(100, round(score, 1))))
        return scores

    @staticmethod
    def _detect_cold_extrusion_onset(results):
        """Find the flow where thermal stress first crosses the
        'yellow' threshold (>= 30). This is purely informational and
        does not affect any test logic."""
        for flow, score in TMCFlowTest._compute_thermal_stress_per_step(
                results):
            if score is not None and score >= 30:
                return flow
        return None

    @staticmethod
    def _compute_intra_run_trend(run_sg, sg2_driver=True):
        """Compute time-trend metrics from one run's SG samples.

        Within a single 5-second run, samples are recorded in time
        order (~20 Hz). If SG drifts during the run it tells us
        something the per-run-median can't: a steady slope is the
        signature of cold extrusion (load grows as filament cools
        further), while a sudden jump near the end is motor slip
        (mechanical limit hit).

        Returns dict with:
            slope          — linear regression slope of SG vs sample index
            early_avg      — mean of first third of samples
            late_avg       — mean of last third
            drift_pct      — (late_avg - early_avg) / median * 100
            drift_pct_load — same, oriented so positive = load increasing
                             (negative SG drift on SG2 drivers, positive on SG4)
            n_samples      — count after warmup-skip
        Returns None if too few samples.
        """
        n_raw = len(run_sg)
        if n_raw < 20:  # need at least ~1 second of samples
            return None
        # Skip first 10 % as warmup (motor speed-up, sensor settling)
        skip = max(2, n_raw // 10)
        samples = run_sg[skip:]
        n = len(samples)
        if n < 10:
            return None

        # Linear regression slope (SG units per sample-index)
        mean_x = (n - 1) / 2.0
        mean_y = sum(samples) / n
        num = sum((i - mean_x) * (s - mean_y)
                  for i, s in enumerate(samples))
        den = sum((i - mean_x) ** 2 for i in range(n))
        slope = (num / den) if den > 0 else 0.0

        # Early vs late thirds
        third = max(1, n // 3)
        early = samples[:third]
        late = samples[-third:]
        early_avg = sum(early) / len(early)
        late_avg = sum(late) / len(late)
        drift = late_avg - early_avg
        median_approx = mean_y if mean_y > 1 else 1.0
        drift_pct = (drift / median_approx * 100.0)

        # Orient drift so POSITIVE = load INCREASING during the run
        # (which is the cold-extrusion signature).
        # SG2 drivers (TMC5160): SG goes DOWN as load increases
        # SG4 drivers (TMC2209/2240): SG goes UP as load increases
        if sg2_driver:
            drift_pct_load = -drift_pct  # invert
        else:
            drift_pct_load = drift_pct

        return {
            'slope': slope,
            'early_avg': early_avg,
            'late_avg': late_avg,
            'drift_pct': drift_pct,
            'drift_pct_load': drift_pct_load,
            'n_samples': n,
        }

    @staticmethod
    def _aggregate_intra_run_trends(per_run_trends):
        """Reduce per-run trend dicts to a step-level aggregate.

        Returns aggregate dict or None if no valid per-run data.
        Keys:
            mean_drift_load     — average load-direction drift across runs
                                   (positive = load increased during runs)
            max_drift_load      — single largest load-increasing drift
            consistent_negative — count of runs where load clearly increased
                                   (drift_load > 1 %), suggests cold-extrusion
            n_valid_runs        — number of runs that had usable data
        """
        valid = [t for t in per_run_trends if t is not None]
        if not valid:
            return None
        load_drifts = [t['drift_pct_load'] for t in valid]
        return {
            'mean_drift_load': sum(load_drifts) / len(load_drifts),
            'max_drift_load': max(load_drifts),
            'consistent_growing_load':
                sum(1 for d in load_drifts if d > 1.0),
            'n_valid_runs': len(valid),
        }

    def _measure_step(self, gcmd, target_flow, step_duration, repeat,
                      skip_warmup=True):
        """Run a single flow measurement (multiple repetitions, aggregate)."""
        mm_per_sec = target_flow / self.filament_area
        feed_rate = mm_per_sec * 60.0
        extrude_length = mm_per_sec * step_duration

        per_run_sg = []
        run_sg_avgs = []
        per_run_trends = []  # intra-run drift metrics
        # Thermal samples now taken DURING extrusion (via the sample
        # timer) and recovery samples taken BETWEEN reps. Old design
        # took snapshots before+after each rep, but those mixed
        # extrusion-stress with recovery values from the cooldown
        # gap, giving misleading averages.
        active_thermal_samples = []   # samples during active extrusion
        recovery_thermal_samples = []  # samples between reps (idle)

        for rep in range(repeat):
            # Capture ONE recovery snapshot just before extrusion starts
            # (gives us the heater's idle/recovery state for diagnostics)
            recovery_thermal_samples.append(self._get_thermal_snapshot())

            self._start_sampling()
            try:
                self.gcode.run_script_from_command(
                    "G1 E%.4f F%.1f\nM400" % (extrude_length, feed_rate))
            finally:
                self._stop_sampling()

            # Pull thermal samples that were captured DURING the
            # extrusion (4 Hz cadence) — these are the only ones that
            # represent actual heater stress under load.
            active_thermal_samples.extend(self.samples_thermal)

            run_sg = list(self.samples_sg)
            per_run_sg.append(run_sg)
            if run_sg:
                run_sg_avgs.append(sum(run_sg) / len(run_sg))

            # Compute intra-run trend metrics (Phase 3: detect graduel
            # load increase within a single run — the signature of cold
            # extrusion as opposed to abrupt motor slip).
            per_run_trends.append(
                self._compute_intra_run_trend(
                    run_sg, sg2_driver=self.sg2_driver))

            if rep < repeat - 1:
                self.gcode.run_script_from_command("G4 P300")

        # Warmup-skip: if first run deviates more than the profile's
        # WARMUP_DRIFT_THRESHOLD from the rest, exclude it. The
        # threshold is driver-specific because run-to-run drift
        # varies significantly between TMC chip families:
        #   TMC5160: ~10 % default (validated against historical CSVs)
        #   TMC2240: 4 %  (the chip shows systematic 3-6 % drift on run 1)
        warmup_dropped = False
        included_indices = list(range(len(run_sg_avgs)))
        if skip_warmup and len(run_sg_avgs) >= 3:
            run1 = run_sg_avgs[0]
            rest_mean = sum(run_sg_avgs[1:]) / len(run_sg_avgs[1:])
            if rest_mean > 0:
                deviation = abs(run1 - rest_mean) / rest_mean
                if deviation > self.profile.WARMUP_DRIFT_THRESHOLD:
                    warmup_dropped = True
                    included_indices = list(range(1, len(run_sg_avgs)))

        # Aggregate from included runs
        agg_sg = []
        for idx in included_indices:
            agg_sg.extend(per_run_sg[idx])

        sg_stats = self._stats(agg_sg)

        included_sg_avgs = [run_sg_avgs[i] for i in included_indices
                            if i < len(run_sg_avgs)]
        run_consistency = None
        if len(included_sg_avgs) > 1:
            sg_run_std = _pstdev(included_sg_avgs)
            sg_run_mean = sum(included_sg_avgs) / len(included_sg_avgs)
            sg_cv = (sg_run_std / sg_run_mean * 100.0
                     if sg_run_mean > 0 else 0)
            run_consistency = {
                'sg_run_std': sg_run_std,
                'sg_cv': sg_cv,
                'warmup_dropped': warmup_dropped,
            }

        # Compact summary line
        sg_med_str = "%.0f" % sg_stats['median'] if sg_stats else 'n/a'
        cv_str = ("%.1f%%" % run_consistency['sg_cv']
                  if run_consistency else 'n/a')
        warmup_str = ' [run 1 excluded as warmup]' if warmup_dropped else ''
        gcmd.respond_info(
            "  %.1f mm³/s | SG median = %s | "
            "run-to-run CV = %s%s"
            % (target_flow, sg_med_str, cv_str, warmup_str))

        # Aggregate thermal samples — only the ACTIVE extrusion ones
        # define pwm_avg/max/min and the temp_drop. Recovery samples
        # are kept for diagnostics (did the heater recover between reps?)
        thermal_agg = self._aggregate_thermal_samples(active_thermal_samples)
        # Augment with recovery diagnostics (mainly: did the heater
        # come back to target between reps?)
        if recovery_thermal_samples and thermal_agg:
            recovery_temps = [s['temp_actual']
                              for s in recovery_thermal_samples
                              if s.get('temp_actual') is not None]
            recovery_targets = [s['temp_target']
                                for s in recovery_thermal_samples
                                if s.get('temp_target') is not None]
            if recovery_temps and recovery_targets:
                # How far below target was the heater at the start of
                # each rep (averaged)? Indicates whether cooldown was
                # enough, or heater is struggling between extrusions.
                deltas = [(t - a) for a, t in
                          zip(recovery_temps, recovery_targets)]
                thermal_agg['recovery_deficit_avg'] = (
                    sum(deltas) / len(deltas))
                thermal_agg['recovery_deficit_max'] = max(deltas)

        return {
            'flow': target_flow,
            'sg': sg_stats,
            'run_consistency': run_consistency,
            'run_sg_avgs': run_sg_avgs,
            'warmup_dropped': warmup_dropped,
            'thermal': thermal_agg,
            'intra_run': self._aggregate_intra_run_trends(per_run_trends),
        }

    # ─── Trigger detection — SG-only mode ──────────────────────────

    def _sg_min_informative(self):
        """Lowest SG value still considered informative.

        For SG4 drivers (TMC2240/2209) low SG values mean 'almost no
        load' and are noisy; we ignore them. For SG2 drivers (TMC5160 et
        al.) low SG means HIGH load — exactly the regime we care about
        — so don't gate on it.
        """
        if self.sg2_driver:
            return -1  # never reject on this gate
        return SG_MIN_INFORMATIVE

    def _sg_jump_threshold(self):
        """Minimum raw |SG delta| considered a real (non-noise) jump.

        Uses the driver-specific profile value, which captures the
        relationship between SG scale and jump magnitudes for that
        chip family.
        """
        return self.profile.SG_JUMP_THRESHOLD

    def _check_triggers_sg(self, results):
        """SG-based slip detection.

        SG can either RISE or FALL with increasing motor load depending on
        driver chip, chopper mode, motor, and CoolStep state. Rather than
        hard-coding a direction per driver, we let the data decide:
        average sign of the recent step-to-step deltas tells us which way
        SG is trending. A slip then shows up as a sharp move in the
        OPPOSITE direction (motor decouples from load → SG snaps back to
        its 'no-load' value).

        Two triggers, both fire only when stepping UP:
          1. SG reload jump: SG moves against the established trend by
             more than the typical step magnitude (and >15 raw units)
          2. Plateau over 2 steps: the trend stalls — cumulative
             trend-direction movement is less than half the typical
        """
        if not results or len(results) < 5:
            return None
        r = results[-1]
        sg_stats = r['sg']
        if sg_stats is None or sg_stats['n'] == 0:
            return None
        sg_med = sg_stats['median']
        target_flow = r['flow']
        sg_label = self._get_sg_label()

        if sg_med <= self._sg_min_informative():
            return None

        prev_flow = results[-2].get('flow', 0)
        going_up = target_flow >= prev_flow - 0.001
        # SG trend triggers (snap-back, abnormal jump) are direction-
        # sensitive — they require a monotonic up-sweep to compare
        # against. Bisection probes can step DOWN, so we skip the
        # trend triggers in that case but still run the
        # direction-independent CV-spike and IQR-spread checks.

        # ─── Trend-direction triggers (snap-back / over-jump / plateau)
        # ─── only valid during monotonic up-sweep (coarse phase). In
        # ─── bisection, the trend baseline is meaningless because we
        # ─── jump around the slip point.
        if going_up:
            # Raw step-to-step SG deltas from recent history.
            sg_deltas = []
            for j in range(max(1, len(results) - 5), len(results) - 1):
                rj, rj_prev = results[j], results[j-1]
                if rj.get('sg') and rj_prev.get('sg'):
                    sg_deltas.append(
                        rj['sg']['median'] - rj_prev['sg']['median'])

            if len(sg_deltas) >= 3:
                # Use median (not mean) of prior deltas as the
                # baseline. Median is robust against outliers like
                # the saturation-region first delta — e.g. starting
                # at flow=10 with a high SGT can produce a -418
                # delta that poisons mean-based predictions for
                # several steps and makes the natural saturation
                # curve look like a "plateau" trigger.
                sorted_deltas = sorted(sg_deltas)
                expected_delta = sorted_deltas[len(sorted_deltas) // 2]
                actual_delta = sg_med - results[-2]['sg']['median']

                # Trend direction (+1 = SG rises with load, -1 = SG falls
                # with load, 0 = flat / inconclusive). Use 1 raw unit as
                # the noise-floor so a tiny non-zero average doesn't lock
                # us into a spurious sign.
                if expected_delta > 1.0:
                    trend_sign = +1
                elif expected_delta < -1.0:
                    trend_sign = -1
                else:
                    trend_sign = 0

                # In trend-direction terms, slip can manifest two ways:
                #   • "Over-jump"   — load_signal jumps in the same
                #                      direction as the trend, but >2×
                #                      the typical step. This is what
                #                      TMC2240/2209 (SG4) often show
                #                      empirically: SG keeps rising but
                #                      suddenly MUCH faster than before.
                #   • "Snap-back"   — load_signal moves OPPOSITE to the
                #                      trend (motor decouples, SG returns
                #                      toward its no-load value). This is
                #                      the textbook SG2 stall signature
                #                      seen on TMC5160 etc.
                # Both should fire; we test each independently.
                if trend_sign != 0:
                    expected_load = expected_delta * trend_sign     # > 0
                    actual_load = actual_delta * trend_sign         # any sign

                    # Snap-back (slip via decoupling)
                    if (actual_load < -expected_load
                            and abs(actual_delta)
                            > self._sg_jump_threshold()):
                        if trend_sign > 0:
                            return ("%s reload jump: %+.0f (expected to "
                                    "keep rising ~+%.0f) — slip detected"
                                    % (sg_label, actual_delta,
                                       expected_load))
                        return ("%s reload jump: %+.0f (expected to keep "
                                "falling ~-%.0f) — slip detected"
                                % (sg_label, actual_delta, expected_load))

                    # Over-jump (slip via abnormal acceleration)
                    if (actual_load > expected_load * 2.0
                            and abs(actual_delta)
                            > self._sg_jump_threshold()):
                        if trend_sign > 0:
                            return ("%s abnormal jump: %+.0f vs expected "
                                    "~+%.0f (%.1fx larger) — slip detected"
                                    % (sg_label, actual_delta,
                                       expected_load,
                                       actual_load / expected_load))
                        return ("%s abnormal drop: %+.0f vs expected "
                                "~-%.0f (%.1fx larger) — slip detected"
                                % (sg_label, actual_delta, expected_load,
                                   actual_load / expected_load))

                    # Trigger 2: plateau over 2 steps, only meaningful
                    # when the trend is sizeable. The threshold is
                    # driver-specific (PLATEAU_RATIO in the profile)
                    # because the steepness of the SG-vs-load
                    # saturation curve varies between chip families.
                    #
                    # Restricted to the coarse phase only: in
                    # bisection / verify, flow probes can step in
                    # either direction (e.g. 45 → 42 → 44 → 43), and
                    # the trend baseline assumption breaks down. The
                    # `going_up` check above doesn't catch this
                    # because individual bisect probes are still
                    # locally upward — but the prior-step deltas in
                    # bisection mode reflect bisection bracket logic,
                    # not the smooth coarse sweep this trigger
                    # assumes.
                    #
                    # Also skip plateau if any prior coarse step had
                    # SG values in the saturation region (close to
                    # 1023). Saturation clipping artificially inflates
                    # the prior delta — the SG-vs-load curve looks
                    # MUCH steeper than reality, so the trigger then
                    # interprets normal saturation behaviour later in
                    # the sweep as a "stalled trend".
                    last_phase = results[-1].get('phase', 'coarse')
                    prior_sg_max = max(
                        results[j]['sg']['median']
                        for j in range(max(0, len(results) - 5),
                                       len(results) - 1)
                        if results[j].get('sg'))
                    saturation_skip = (
                        prior_sg_max
                        > self.profile.PLATEAU_SATURATION_SKIP)
                    if (expected_load > 5
                            and last_phase == 'coarse'
                            and not saturation_skip):
                        # Single-step plateau check (NEW): if the most
                        # recent step's SG-delta is essentially flat
                        # compared to the recent baseline, fire even
                        # without needing a 2-step cumulative window.
                        # An abrupt plateau (e.g. typical -25 deltas
                        # then suddenly +0.5) is a strong slip signal
                        # and shouldn't be smoothed over 2 steps.
                        # Threshold is half the PLATEAU_RATIO — must
                        # be a much clearer single-step plateau to
                        # fire than a borderline 2-step accumulation.
                        single_threshold_factor = (
                            self.profile.PLATEAU_RATIO / 2.0)
                        single_threshold = (expected_load
                                            * single_threshold_factor)
                        if actual_load < single_threshold:
                            direction = ("rising" if trend_sign > 0
                                         else "falling")
                            return ("%s single-step plateau: %s trend "
                                    "flattened abruptly (only %.0f vs "
                                    "expected %.0f load-units in this "
                                    "step) — strong sign that flow "
                                    "increase no longer adds motor load"
                                    % (sg_label, direction,
                                       actual_load, expected_load))

                        # Cumulative 2-step check (existing logic)
                        prev_actual = (results[-2]['sg']['median']
                                       - results[-3]['sg']['median'])
                        cumulative_load = (actual_load
                                           + prev_actual * trend_sign)
                        expected_2step = expected_load * 2
                        if cumulative_load < (expected_2step
                                              * self.profile.PLATEAU_RATIO):
                            direction = ("rising" if trend_sign > 0
                                         else "falling")
                            return ("%s plateau over 2 steps: %s trend "
                                    "stalled (only %.0f vs typical %.0f "
                                    "load-units) — flow no longer "
                                    "increasing motor load (slip starting)"
                                    % (sg_label, direction,
                                       cumulative_load, expected_2step))

        # ─── Direction-independent triggers (always evaluated) ───
        # CV-spike and IQR-spread fire on the per-step variance signal
        # itself, not on a trend baseline, so they make sense for both
        # coarse and bisection probes.

        # Trigger 3: run-to-run CV spike. Intermittent slip shows up as
        # a sudden burst of variance between repeats at the same flow,
        # even when the median doesn't move much. Fire when the latest
        # CV is both elevated in absolute terms AND much higher than the
        # baseline of recent steps.
        cv_reason = self._check_cv_spike(results, sg_label)
        if cv_reason:
            return cv_reason

        # Trigger 4: IQR/spread anomaly. Catches "quiet" stalls where
        # the median looks normal but the distribution widens (brief
        # intermittent stall absorbed by median over 5 repeats).
        iqr_reason = self._check_iqr_spread(results, sg_label)
        if iqr_reason:
            return iqr_reason

        # Trigger 5: SG max spike — bisection / verify only. Catches
        # decoupling events where one of the repeats briefly snaps SG
        # to its no-load value, but the median absorbs it.
        spike_reason = self._check_sg_max_spike(results, sg_label)
        if spike_reason:
            return spike_reason

        # Trigger 6: per-run outlier — bisection / verify only. Catches
        # the case where 1 of N repeats has a clearly different SG/CS
        # profile from the others (intermittent slip in just one run).
        outlier_reason = self._check_run_outlier(results, sg_label)
        if outlier_reason:
            return outlier_reason

        return None

    def _check_cv_spike(self, results, sg_label):
        """Detect a run-to-run CV spike — intermittent slip signature.

        A clean motor produces tight repeats (CV usually <3% on stable
        SG2 setups, may go up to 5% on noisier SG4). When the motor
        starts intermittently slipping, individual repeats diverge
        sharply, even if the median across all samples looks 'normal'.

        Triggers (any one fires):
          (a) "high-variance trip" — last CV >= 10% absolute (regardless
              of baseline). Catches sudden chaotic slip.
          (b) "low-baseline jump"  — last CV >= ratio * avg of prior
              N steps AND last CV >= absolute floor. Ratio and floor
              are tighter in coarse, looser in bisection (where we're
              already near slip).
          (c) "rising CV trend"   — CV grew across 2 consecutive
              steps (each >=1.3x prior).
          (d) "bisection absolute" — in bisection / verify, ANY CV
              >= 2x the *coarse-phase median CV* (or >= 5% absolute,
              whichever is higher). Catches the case where
              intermittent slip already started during coarse and
              "polluted" the immediate baseline, making patterns (b)
              and (c) under-react.

        Baseline strategy: when we're in bisection/verify, the most
        meaningful baseline is the *coarse phase* — it represents the
        clean, slip-free motor behaviour. Using only the immediate 3
        prior steps as baseline lets earlier elevated bisection CVs
        raise the threshold and mask later anomalies.
        """
        last = results[-1]
        last_rc = last.get('run_consistency') or {}
        if 'sg_cv' not in last_rc:
            return None
        last_cv = last_rc['sg_cv']

        last_phase = last.get('phase', 'coarse')
        in_bisection = last_phase in ('bisect', 'verify')

        # Build two baselines:
        #   - "immediate": last 3 steps regardless of phase (sensitive
        #     to recent trend)
        #   - "coarse": median of CVs from the COARSE phase only
        #     (slip-free reference, only available once we have data)
        prior_cvs = []
        for r in results[-4:-1]:
            rc = r.get('run_consistency') or {}
            if 'sg_cv' in rc:
                prior_cvs.append(rc['sg_cv'])

        coarse_cvs = []
        for r in results[:-1]:
            if r.get('phase', 'coarse') != 'coarse':
                continue
            rc = r.get('run_consistency') or {}
            if 'sg_cv' in rc:
                coarse_cvs.append(rc['sg_cv'])
        # Median of coarse CVs, excluding the LAST coarse step (which
        # may be the elevated step that triggered bisection).
        coarse_baseline_cv = None
        if len(coarse_cvs) >= 4:
            sorted_cv = sorted(coarse_cvs[:-1])
            n = len(sorted_cv)
            coarse_baseline_cv = (sorted_cv[n // 2] if n % 2
                                  else (sorted_cv[n // 2 - 1]
                                        + sorted_cv[n // 2]) / 2)

        # Pattern (a): high-variance trip — works without baseline
        if last_cv >= self.profile.CV_HIGH_VARIANCE:
            if not prior_cvs:
                return ("run-to-run %s CV spiked to %.1f%% — repeats "
                        "diverging, intermittent slip"
                        % (sg_label, last_cv))
            avg_prior_cv = sum(prior_cvs) / len(prior_cvs)
            if last_cv >= self.profile.CV_LOWBASE_RATIO * avg_prior_cv:
                return ("run-to-run %s CV spiked to %.1f%% (baseline "
                        "~%.1f%% over previous 3 steps) — repeats "
                        "diverging, intermittent slip"
                        % (sg_label, last_cv, avg_prior_cv))

        if len(prior_cvs) < 3:
            return None
        avg_prior_cv = sum(prior_cvs) / len(prior_cvs)

        # Pattern (b): low-baseline jump vs immediate prior
        b_ratio = (self.profile.CV_JUMP_RATIO_BISECT if in_bisection
                   else self.profile.CV_JUMP_RATIO_COARSE)
        b_min_cv = (self.profile.CV_JUMP_MIN_BISECT if in_bisection
                    else self.profile.CV_JUMP_MIN_COARSE)
        if last_cv >= b_min_cv and last_cv >= b_ratio * avg_prior_cv:
            return ("run-to-run %s CV jumped to %.1f%% (%.1fx baseline "
                    "of %.1f%% over previous 3 steps) — repeats "
                    "diverging, intermittent slip"
                    % (sg_label, last_cv,
                       last_cv / avg_prior_cv, avg_prior_cv))

        # Pattern (c): rising CV trend across 2 consecutive steps.
        c_min_cv = (self.profile.CV_RISING_MIN_LAST_BISECT if in_bisection
                    else self.profile.CV_RISING_MIN_LAST_COARSE)
        c_ratio = self.profile.CV_RISING_RATIO
        if (last_cv >= c_min_cv
                and len(prior_cvs) >= 2
                and prior_cvs[-1] >= c_ratio * prior_cvs[-2]
                and last_cv >= c_ratio * prior_cvs[-1]
                and prior_cvs[-1] >= self.profile.CV_RISING_MIN_PRIOR):
            return ("run-to-run %s CV rising across 3 steps "
                    "(%.1f%% → %.1f%% → %.1f%%) — gradual slip onset"
                    % (sg_label, prior_cvs[-2], prior_cvs[-1], last_cv))

        # Pattern (d): bisection absolute vs coarse baseline.
        # In bisection/verify, compare against the slip-free coarse
        # phase median. This catches cases where one elevated bisect
        # step has already pulled the immediate baseline up, masking
        # subsequent elevated values.
        if (in_bisection
                and coarse_baseline_cv is not None
                and last_cv >= self.profile.CV_VS_COARSE_MIN
                and last_cv >=
                    self.profile.CV_VS_COARSE_RATIO * coarse_baseline_cv):
            return ("run-to-run %s CV %.1f%% in bisection vs coarse-"
                    "phase baseline %.1f%% (%.1fx) — slip signature "
                    "above clean-extrusion noise floor"
                    % (sg_label, last_cv, coarse_baseline_cv,
                       last_cv / coarse_baseline_cv))

        return None

    def _check_iqr_spread(self, results, sg_label):
        """Detect an IQR/spread anomaly — quiet stall signature.

        Sometimes a motor stalls briefly (start or end of move) and
        recovers, leaving a median that looks normal but a wider
        distribution. The IQR (P75-P25) widens when this happens.

        Four triggers, in order:
          (a) "ratio vs immediate" — current IQR >= ratio * avg of
              prior 3 IQRs. Ratio is 3.0 in coarse, 1.7 in bisection.
          (b) "ratio vs coarse baseline" (bisection only) — current
              IQR >= 2.0 * the median IQR from the COARSE phase.
              Catches cases where earlier elevated bisect IQRs have
              raised the immediate baseline and masked subsequent
              anomalies.
          (c) "absolute" — current IQR >= 25 raw units in bisection /
              verify. A motor near slip should have median, P25 and
              P75 within a few units of each other; an IQR of 25+
              indicates intermittent stall regardless of context.
          (d) "cumulative growth" (coarse only) — IQR has roughly
              doubled relative to the early-test baseline (median of
              first 3 coarse IQRs) AND now exceeds an absolute floor.
              Catches GRADUAL widening that single-step ratios miss.
              Strong pre-slip indicator: stick-slip events build up
              over multiple steps before the abrupt fail.
        """
        last = results[-1]
        sg_stats = last.get('sg') or {}
        if 'p25' not in sg_stats or 'p75' not in sg_stats:
            return None
        last_iqr = sg_stats['p75'] - sg_stats['p25']

        prior_iqrs = []
        for r in results[-4:-1]:
            sg_p = r.get('sg') or {}
            if 'p25' in sg_p and 'p75' in sg_p:
                prior_iqrs.append(sg_p['p75'] - sg_p['p25'])

        last_phase = last.get('phase', 'coarse')
        in_bisection = last_phase in ('bisect', 'verify')

        # (c) absolute — fires unconditionally on any genuinely large
        # IQR during bisection / verify. Cheap and reliable.
        if in_bisection and last_iqr >= self.profile.IQR_ABSOLUTE_TRIGGER:
            return ("%s spread widened: IQR %.0f raw units in %s — "
                    "samples no longer cluster, intermittent stall"
                    % (sg_label, last_iqr, last_phase))

        # (d) cumulative growth — coarse phase only. Catches gradually
        # widening IQR over many steps (typical slow stick-slip onset
        # that single-step ratio triggers miss). Requires at least 5
        # coarse steps of history so the early baseline is meaningful.
        if not in_bisection:
            coarse_iqrs = []
            for r in results[:-1]:
                if r.get('phase', 'coarse') != 'coarse':
                    continue
                sg_p = r.get('sg') or {}
                if 'p25' in sg_p and 'p75' in sg_p:
                    coarse_iqrs.append(sg_p['p75'] - sg_p['p25'])
            # Need enough history for a stable baseline + clear margin
            # before the current step.
            if len(coarse_iqrs) >= 5:
                # Early baseline: median of first 3 IQRs (after the
                # very first which can be warmup-noisy)
                early_iqrs = sorted(coarse_iqrs[:4])
                # take the 2nd and 3rd smallest as a robust early baseline
                early_baseline = (early_iqrs[1] + early_iqrs[2]) / 2.0
                # Threshold: IQR has ≥ 2× early baseline AND exceeds
                # an absolute floor (so very-small early IQRs of 2-3
                # don't trigger on noise alone). Floor is set
                # deliberately above IQR_RATIO_MIN_ABS so this trigger
                # is independent of the per-step ratio path.
                growth_floor_abs = max(
                    self.profile.IQR_RATIO_MIN_ABS, 12)
                if (early_baseline >= 0.5
                        and last_iqr >= growth_floor_abs
                        and last_iqr >= 2.0 * early_baseline):
                    return ("%s spread grew over sweep: IQR climbed "
                            "from baseline ~%.0f (early steps) to %.0f "
                            "(%.1fx) — gradual pre-slip widening, motor "
                            "is stuttering more often as flow rises"
                            % (sg_label, early_baseline, last_iqr,
                               last_iqr / early_baseline))

        if len(prior_iqrs) < 3:
            return None
        avg_prior_iqr = sum(prior_iqrs) / len(prior_iqrs)
        if avg_prior_iqr < 1:
            return None

        # (a) ratio vs immediate prior steps.
        # If the profile requires CV cross-check (e.g. SG4 drivers with
        # active CoolStep), confirm with the run-to-run CV — without
        # elevated CV, a wide IQR is likely a CoolStep-current-transition
        # effect, not slip. Genuine slip raises both within-step spread
        # AND run-to-run variance.
        ratio_threshold = (self.profile.IQR_RATIO_BISECT if in_bisection
                           else self.profile.IQR_RATIO_COARSE)
        if (last_iqr >= self.profile.IQR_RATIO_MIN_ABS
                and last_iqr >= ratio_threshold * avg_prior_iqr):
            cv_confirms = True
            cv_note = ""
            if self.profile.IQR_RATIO_REQUIRE_CV:
                last_rc = last.get('run_consistency') or {}
                last_cv_a = last_rc.get('sg_cv')
                cv_floor = (self.profile.IQR_RATIO_CV_FLOOR_BISECT
                            if in_bisection
                            else self.profile.IQR_RATIO_CV_FLOOR_COARSE)
                # Build prior-CV baseline same way as prior-IQR baseline
                prior_cvs_a = []
                for r in results[-4:-1]:
                    rc = r.get('run_consistency') or {}
                    if 'sg_cv' in rc:
                        prior_cvs_a.append(rc['sg_cv'])
                avg_prior_cv = (sum(prior_cvs_a) / len(prior_cvs_a)
                                if prior_cvs_a else 0.0)
                # CV must be elevated above absolute floor AND above
                # baseline (or baseline must be tiny). 1.4× ratio is
                # less strict than the value-based jump triggers in
                # _check_cv_spike — IQR pattern (a) only needs CV to
                # *not contradict* the spread anomaly.
                cv_confirms = (last_cv_a is not None
                               and last_cv_a >= cv_floor
                               and (avg_prior_cv < 0.5
                                    or last_cv_a >= 1.4 * avg_prior_cv))
                if cv_confirms and last_cv_a is not None:
                    cv_note = (" (CV %.1f%% confirms, prior ~%.1f%%)"
                               % (last_cv_a, avg_prior_cv))
            if cv_confirms:
                return ("%s spread widened: IQR %.0f vs baseline ~%.0f "
                        "(%.1fx) — quiet/intermittent stall in samples"
                        "%s"
                        % (sg_label, last_iqr, avg_prior_iqr,
                           last_iqr / avg_prior_iqr, cv_note))

        # (b) ratio vs coarse baseline (bisection only).
        # Cross-check with CV: a pure spread anomaly with NORMAL CV is
        # often just statistical fluctuation (e.g. a single outlying
        # sample stretched the IQR but the run-to-run averages are
        # tight). Genuine slip widens BOTH spread AND CV. So we only
        # fire when CV also confirms elevated noise relative to coarse.
        if in_bisection:
            coarse_iqrs = []
            coarse_cvs = []
            for r in results[:-1]:
                if r.get('phase', 'coarse') != 'coarse':
                    continue
                sg_p = r.get('sg') or {}
                if 'p25' in sg_p and 'p75' in sg_p:
                    coarse_iqrs.append(sg_p['p75'] - sg_p['p25'])
                rc = r.get('run_consistency') or {}
                if 'sg_cv' in rc:
                    coarse_cvs.append(rc['sg_cv'])
            if len(coarse_iqrs) >= 4 and len(coarse_cvs) >= 4:
                # Median of all but the last coarse step (which may be
                # the elevated step that triggered bisection).
                sorted_iqr = sorted(coarse_iqrs[:-1])
                n = len(sorted_iqr)
                median_coarse_iqr = (sorted_iqr[n // 2] if n % 2
                                     else (sorted_iqr[n // 2 - 1]
                                           + sorted_iqr[n // 2]) / 2)
                sorted_cv = sorted(coarse_cvs[:-1])
                m = len(sorted_cv)
                median_coarse_cv = (sorted_cv[m // 2] if m % 2
                                    else (sorted_cv[m // 2 - 1]
                                          + sorted_cv[m // 2]) / 2)
                last_rc = last.get('run_consistency') or {}
                last_cv = last_rc.get('sg_cv', 0.0)
                # CV cross-check: must be elevated vs coarse AND above
                # an absolute floor. If CV is below baseline-ish, it's
                # not slip — it's a wide-distribution fluke.
                if self.profile.IQR_VS_COARSE_REQUIRE_CV:
                    cv_confirms = (
                        last_cv >= self.profile.IQR_VS_COARSE_CV_FLOOR
                        and median_coarse_cv >= 0.5
                        and last_cv >=
                            (self.profile.IQR_VS_COARSE_CV_RATIO
                             * median_coarse_cv))
                else:
                    cv_confirms = True
                if (median_coarse_iqr >= 1
                        and last_iqr >= self.profile.IQR_VS_COARSE_MIN_ABS
                        and last_iqr >=
                            (self.profile.IQR_VS_COARSE_RATIO
                             * median_coarse_iqr)
                        and cv_confirms):
                    return ("%s spread IQR %.0f in %s vs coarse-phase "
                            "median IQR ~%.0f (%.1fx); CV %.1f%% also "
                            "elevated vs coarse %.1f%% — slip widening"
                            % (sg_label, last_iqr, last_phase,
                               median_coarse_iqr,
                               last_iqr / median_coarse_iqr,
                               last_cv, median_coarse_cv))

        return None

    def _check_sg_max_spike(self, results, sg_label):
        """Detect a no-load decoupling spike inside a single step.

        When the motor briefly decouples from the load during one of
        the repeats, SG snaps toward its no-load value. The 5-second
        per-run average + median over 5 runs absorbs the brief spike,
        but sg_max / sg_min records it.

        Direction is data-driven, not driver-hardcoded: across all
        three driver families (TMC5160 SG2, TMC2240 SG4, TMC2209 SG4)
        SG normally FALLS as load increases. So decoupling = SG snaps
        UP toward the no-load value, which means sg_max is the right
        signal regardless of driver. (We still keep a sg_min check as
        fallback in case a particular setup runs the other direction.)

        Only fires in bisection / verify, since coarse has plenty of
        other triggers and natural variation is wider there.
        """
        last = results[-1]
        if last.get('phase') not in ('bisect', 'verify'):
            return None
        sg = last.get('sg') or {}
        if 'max' not in sg or 'min' not in sg or 'median' not in sg:
            return None
        last_max = sg['max']
        last_min = sg['min']
        last_med = sg['median']

        # Build baseline of "typical" sg_max and sg_min from the
        # coarse phase. We need both because the no-load direction
        # depends on the actual SG-vs-load slope of THIS hardware.
        coarse_maxes = []
        coarse_mins = []
        coarse_meds = []
        for r in results[:-1]:
            if r.get('phase', 'coarse') != 'coarse':
                continue
            sg_p = r.get('sg') or {}
            if 'max' in sg_p:
                coarse_maxes.append(sg_p['max'])
            if 'min' in sg_p:
                coarse_mins.append(sg_p['min'])
            if 'median' in sg_p:
                coarse_meds.append(sg_p['median'])
        if len(coarse_maxes) < 4 or len(coarse_meds) < 4:
            return None

        # Determine SG-vs-load trend direction: compare first vs last
        # coarse-phase median (excluding the last coarse step which
        # likely triggered). If SG dropped from start to end, normal
        # = falling-with-load → decoupling = max spikes UP. Otherwise
        # = rising-with-load → decoupling = min drops DOWN.
        first_med = coarse_meds[0]
        # Use median of last 2-3 (excluding very last, which triggered)
        if len(coarse_meds) >= 3:
            late_meds = coarse_meds[-3:-1] if len(coarse_meds) > 3 else coarse_meds[-2:-1]
            late_med = sum(late_meds) / len(late_meds)
        else:
            late_med = coarse_meds[-1]
        sg_falls_with_load = late_med < first_med

        def _median(xs):
            s = sorted(xs); n = len(s)
            return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2

        if sg_falls_with_load:
            # Normal case for all common driver setups. Decoupling →
            # SG snaps UP. Compare sg_max to typical coarse sg_max.
            median_coarse_max = _median(coarse_maxes[:-1])
            # Two paths to fire:
            # (a) Strong ratio + above coarse: max is dramatically
            #     above median AND clearly above what coarse saw.
            # (b) Big absolute jump: max is far above median AND the
            #     gap (max - median) is large. This covers narrow-range
            #     drivers (e.g. TMC2209) where coarse_max can already
            #     sit near the no-load value.
            absolute_floor = (self.profile.SG_MAX_ABS_GAP
                              if self.sg2_driver
                              else self.profile.SG_MAX_ABS_GAP_SG4)
            ratio_vs_coarse = (last_max
                               / max(median_coarse_max, 1))
            ratio_vs_med = last_max / max(last_med, 1)
            big_gap = (last_max - last_med) >= self.profile.SG_MAX_BIG_GAP
            fires_a = (ratio_vs_med >= self.profile.SG_MAX_RATIO_TO_MEDIAN
                       and ratio_vs_coarse >=
                            self.profile.SG_MAX_RATIO_TO_COARSE
                       and last_max - last_med >= absolute_floor)
            fires_b = (ratio_vs_med >= self.profile.SG_MAX_BIG_RATIO
                       and big_gap)
            if fires_a or fires_b:
                return ("%s max %.0f in this step is %.1fx the "
                        "median (%.0f) and %.1fx the typical coarse "
                        "max (%.0f) — at least one repeat showed "
                        "motor decoupling spike (no-load value)"
                        % (sg_label, last_max, ratio_vs_med, last_med,
                           ratio_vs_coarse, median_coarse_max))
        else:
            # Rare case: SG rises with load. Decoupling → SG snaps
            # DOWN. Compare sg_min to typical coarse sg_min.
            if not coarse_mins:
                return None
            median_coarse_min = _median(coarse_mins[:-1])
            if (last_min > 0
                    and last_min <=
                        last_med / self.profile.SG_MIN_RATIO_TO_MEDIAN
                    and last_med - last_min >=
                        self.profile.SG_MIN_ABS_GAP):
                return ("%s min %.0f is %.1fx below the median (%.0f)"
                        " — at least one repeat dropped to no-load "
                        "value (decoupling)"
                        % (sg_label, last_min,
                           last_med / max(last_min, 1), last_med))

        return None

    def _check_run_outlier(self, results, sg_label):
        """Detect when one of the per-run averages is an outlier.

        Each measurement step runs N repeats (default 5). We have the
        list of per-run SG averages. If the run-to-run variance is
        dominated by a SINGLE outlying run (one repeat much off, the
        others tight), the median absorbs it but it's clear evidence
        of intermittent slip.

        Detection:
          - At least 4 valid runs
          - Find the run whose SG_avg deviates most from the median of
            the other runs
          - If that deviation is > 4x the median absolute deviation
            (MAD) of the rest AND >= 8 % of the median, it's an outlier

        If warmup was dropped during stats aggregation (run 1 deviated
        too much from the rest), this check operates only on the
        warmup-included runs (runs 2..N) — otherwise it would
        re-flag the warmup run as an outlier, defeating the purpose
        of the warmup-skip.

        Only fires in bisect or verify (where we care about
        reproducibility, not coarse exploration).
        """
        last = results[-1]
        if last.get('phase') not in ('bisect', 'verify'):
            return None
        all_run_sg = last.get('run_sg_avgs') or []
        warmup_dropped = last.get('warmup_dropped', False)

        # If warmup was excluded from stats, also exclude it from
        # outlier analysis. The displayed run number compensates for
        # the offset so reports refer to the original run index.
        if warmup_dropped and len(all_run_sg) >= 1:
            run_sg = all_run_sg[1:]
            run_idx_offset = 1
        else:
            run_sg = all_run_sg
            run_idx_offset = 0

        if len(run_sg) < 4:
            return None

        # Find the run furthest from the median of the rest
        n = len(run_sg)
        outlier_idx = -1
        outlier_dev = 0.0
        median_rest = 0.0
        mad_rest = 0.0
        for i in range(n):
            others = [run_sg[j] for j in range(n) if j != i]
            sorted_o = sorted(others)
            m = len(sorted_o)
            med_o = (sorted_o[m // 2] if m % 2
                     else (sorted_o[m // 2 - 1] + sorted_o[m // 2]) / 2)
            devs = [abs(x - med_o) for x in others]
            sorted_d = sorted(devs)
            mad_o = (sorted_d[m // 2] if m % 2
                     else (sorted_d[m // 2 - 1] + sorted_d[m // 2]) / 2)
            dev_i = abs(run_sg[i] - med_o)
            if dev_i > outlier_dev:
                outlier_dev = dev_i
                outlier_idx = i
                median_rest = med_o
                mad_rest = mad_o

        if outlier_idx < 0 or median_rest <= 0:
            return None

        # Outlier criterion: deviation >= 4x MAD AND >= 8% of median
        # (avoids tripping on tiny absolute differences when MAD is
        # near zero).
        if mad_rest < 0.5:
            mad_rest = 0.5  # noise floor
        if not (outlier_dev >= self.profile.OUTLIER_MAD_RATIO * mad_rest
                and outlier_dev >=
                    self.profile.OUTLIER_MIN_REL * median_rest):
            return None

        # SG-only outlier detection. Index reporting uses the
        # original run number (1-based), accounting for the warmup
        # offset so the user can identify which CSV row corresponds.
        original_run_num = outlier_idx + 1 + run_idx_offset
        return ("run %d is an outlier (SG_avg %.1f vs %.1f for the "
                "other repeats, %.1fx MAD) — at least one of the "
                "repeats stalled while the others ran clean"
                % (original_run_num, run_sg[outlier_idx],
           
