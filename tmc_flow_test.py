# TMC Flow Test — Adaptive max-volumetric-flow detection for extruders
# credits:
#   Steven (Fragmon) — Crydteam
#   YouTube: https://www.youtube.com/@crydteamprinting
#
# License: GPLv3

import logging
import math
import os
import statistics
import time
import json

SAMPLE_INTERVAL = 0.05    # 20 Hz polling
MIN_HOTEND_TEMP = 180.0
MODULE_NAME = "TMC Flow Test"
MODULE_VERSION = "3.0"
SG_MIN_INFORMATIVE = 50   # below this SG value, readings are noise


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
        self.sg4_available = False  # SG4_RESULT register (TMC2240)

        # Sample buffers
        self.samples_sg = []
        self.samples_cs = []      # only used in CS mode
        self.samples_time = []
        self.sampling_active = False
        self.sample_timer = None
        self.sample_start_time = 0.0

        # Track which mode we're in for trigger logic
        self._mode = 'sg'         # 'sg' or 'cs'

        # Main command — auto-detects mode from driver_SEMIN
        self.gcode.register_command(
            'TMC_FLOW_FIND_MAX', self.cmd_TMC_FLOW_FIND_MAX,
            desc='Find max volumetric flow rate. Auto-detects test mode '
                 'from driver_SEMIN (CoolStep enabled or disabled)')
        # Legacy / explicit mode commands (still work for existing macros)
        self.gcode.register_command(
            'TMC_FLOW_FIND_MAX_SG', self.cmd_TMC_FLOW_FIND_MAX_SG,
            desc='Force SG-only mode (for setups with CoolStep disabled)')
        self.gcode.register_command(
            'TMC_FLOW_FIND_MAX_CS', self.cmd_TMC_FLOW_FIND_MAX_CS,
            desc='Force CoolStep + StallGuard mode '
                 '(for setups with CoolStep enabled)')
        self.gcode.register_command(
            'TMC_FLOW_STATUS', self.cmd_TMC_FLOW_STATUS,
            desc='Show current TMC StallGuard / CoolStep diagnostic values')

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
                # Check for SG4_RESULT register (TMC2240 only)
                try:
                    self.sg4_available = (
                        self.is_2240
                        and 'SG4_RESULT' in self.tmc.mcu_tmc.name_to_reg)
                except AttributeError:
                    self.sg4_available = False
                logging.info(
                    "tmc_flow_test: using %s for stepper '%s' "
                    "(SG4=%s, is_2209=%s)",
                    drv, self.stepper_name, self.sg4_available, self.is_2209)
                return
        raise self.gcode.error(
            "tmc_flow_test: no TMC driver found for stepper '%s'."
            % self.stepper_name)

    # ─── Driver-specific field accessors ────────────────────────────

    def _get_sg_threshold_field_name(self):
        """Return the correct SG-threshold field name for this driver."""
        if self.is_2240:
            return 'sg4_thrs'
        # TMC2209, TMC2226, TMC5160 (sgt), etc. use 'sgthrs'
        return 'sgthrs'

    def _get_sg_label(self):
        """Human-readable label for the SG signal."""
        if self.is_2240:
            return 'SG4_RESULT'
        return 'SG_RESULT'

    # ─── TMC config validation ──────────────────────────────────────

    def _check_tmc_config(self, mode):
        """Verify TMC driver is configured correctly for the requested mode.

        mode: 'sg' (CoolStep should be OFF: SEMIN=0)
              'cs' (CoolStep should be ON: SEMIN > 0)

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

        # ─── StealthChop check (driver-specific) ───
        # TMC2240: en_pwm_mode = 1 means StealthChop enabled
        # TMC2209: en_spreadCycle = 0 means StealthChop enabled
        stealthchop_active = True
        stealthchop_indicator = None
        if self.is_2240:
            if en_pwm_mode is not None:
                stealthchop_active = (en_pwm_mode == 1)
                stealthchop_indicator = ('en_pwm_mode', en_pwm_mode, 'should be 1')
        elif self.is_2209:
            if en_spread_cycle is not None:
                stealthchop_active = (en_spread_cycle == 0)
                stealthchop_indicator = ('en_spreadCycle', en_spread_cycle,
                                         'should be 0')

        if not stealthchop_active and stealthchop_indicator:
            problems.append(
                (stealthchop_indicator[0], stealthchop_indicator[1],
                 'StealthChop is not active. StallGuard needs StealthChop '
                 'ON.\n'
                 'Add to your [%s extruder] section:\n'
                 '  stealthchop_threshold: 999999' % self.driver_type))
        elif (tpwmthrs is not None and tpwmthrs > 0
              and tpwmthrs < 0x10000):
            # Mid-range tpwmthrs would switch to SpreadCycle at higher speeds
            problems.append(
                ('tpwmthrs', tpwmthrs,
                 'StealthChop only active below a velocity threshold '
                 '(tpwmthrs=%d). At higher flows the driver switches to '
                 'SpreadCycle and breaks StallGuard.\n'
                 'Set:\n'
                 '  stealthchop_threshold: 999999' % tpwmthrs))

        # ─── tcoolthrs check (StallGuard gate) ───
        if tcoolthrs == 0:
            problems.append(
                ('tcoolthrs', tcoolthrs,
                 'StallGuard reading disabled (tcoolthrs=0).\n'
                 'Add to your [%s extruder] section:\n'
                 '  coolstep_threshold: 0.5' % self.driver_type))

        # ─── SG threshold check ───
        if sg_thrs_val == 0:
            problems.append(
                (sg_thrs_field, sg_thrs_val,
                 '%s is 0. StallGuard trigger inactive.\n'
                 'Add to printer.cfg:\n'
                 '  [delayed_gcode setup_extruder_sg]\n'
                 '  initial_duration: 2.0\n'
                 '  gcode:\n'
                 '      SET_TMC_FIELD STEPPER=%s FIELD=%s VALUE=%d'
                 % (sg_thrs_field, self.stepper_name,
                    sg_thrs_field,
                    80 if self.is_2240 else 100)))

        # ─── Mode-specific CoolStep check ───
        if mode == 'sg':
            # SG-only mode requires CoolStep off for SG-only triggers to apply
            if semin is not None and semin != 0:
                problems.append(
                    ('semin', semin,
                     'SG-only mode is meant for setups with CoolStep '
                     'disabled, but driver_SEMIN = %d.\n'
                     'Easiest fix: just run TMC_FLOW_FIND_MAX (without _SG)\n'
                     '— it auto-selects the right mode for your config.\n'
                     'Or change your [%s extruder] section to driver_SEMIN: 0\n'
                     'if you want to test in SG-only mode.'
                     % (semin, self.driver_type)))
            else:
                infos.append(
                    "SG-only mode: CoolStep is disabled (driver_SEMIN=0). "
                    "Motor runs at constant IRUN. Test will use SG-based "
                    "triggers only.")
        elif mode == 'cs':
            # CS mode requires CoolStep on for CS-based triggers to fire
            if semin == 0:
                problems.append(
                    ('semin', semin,
                     'CoolStep mode is meant for setups with CoolStep '
                     'enabled, but driver_SEMIN = 0.\n'
                     'Easiest fix: just run TMC_FLOW_FIND_MAX (without _CS)\n'
                     '— it auto-selects the right mode for your config.\n'
                     'Or change your [%s extruder] section to enable CoolStep:\n'
                     '  driver_SEMIN: 5\n'
                     '  driver_SEMAX: 2\n'
                     '  driver_SEUP: 2\n'
                     '  driver_SEDN: 1\n'
                     '  driver_SEIMIN: 1\n'
                     'if you want to test in CoolStep mode.'
                     % self.driver_type))
            else:
                infos.append(
                    "CoolStep mode: CoolStep is active (driver_SEMIN=%d). "
                    "Motor current adapts to load. Test will use CS-based "
                    "triggers with SG fallback." % semin)

        # Driver info
        if self.is_2209:
            infos.append(
                "Driver: TMC2209 detected (uses SG_RESULT, sgthrs).")
        elif self.is_2240:
            infos.append(
                "Driver: TMC2240 detected (uses SG4_RESULT, sg4_thrs, "
                "sg4_filt_en).")

        return (problems, infos)

    # ─── SG sampling ─────────────────────────────────────────────────

    def _read_sg(self):
        """Read StallGuard value directly from the driver register.

        TMC2240: SG4_RESULT register
        TMC2209: SG_RESULT register
        Other drivers: fallback via get_status()
        """
        reg_name = None
        if self.is_2240:
            reg_name = 'SG4_RESULT'
        elif self.is_2209:
            reg_name = 'SG_RESULT'

        if reg_name is not None:
            try:
                reg_val = self.tmc.mcu_tmc.get_register(reg_name)
                return reg_val & 0x3FF  # lower 10 bits
            except Exception as e:
                logging.debug(
                    "tmc_flow_test: %s read failed: %s", reg_name, e)
                return None
        # Fallback for other drivers (TMC5160 / TMC2130 / etc.)
        try:
            drv = self.tmc.get_status(self.reactor.monotonic())
            if 'drv_status' in drv and isinstance(drv['drv_status'], dict):
                return drv['drv_status'].get('sg_result')
            return drv.get('sg_result')
        except Exception:
            return None

    def _read_cs(self):
        """Read CS_ACTUAL directly from DRV_STATUS register.

        Bits 16-20 of DRV_STATUS = CS_ACTUAL (5 bits, range 0-31).
        Same layout on TMC2240 and TMC2209.
        """
        if self.is_2240 or self.is_2209:
            try:
                reg_val = self.tmc.mcu_tmc.get_register('DRV_STATUS')
                return (reg_val >> 16) & 0x1F
            except Exception as e:
                logging.debug(
                    "tmc_flow_test: DRV_STATUS read failed: %s", e)
                return None
        # Fallback for other drivers
        try:
            drv = self.tmc.get_status(self.reactor.monotonic())
            if 'drv_status' in drv and isinstance(drv['drv_status'], dict):
                return drv['drv_status'].get('cs_actual')
            return drv.get('cs_actual')
        except Exception:
            return None

    def _start_sampling(self, sample_cs=False):
        self.samples_sg = []
        self.samples_cs = []
        self.samples_time = []
        self._sample_cs_flag = sample_cs
        self.sample_start_time = self.reactor.monotonic()
        self.sampling_active = True
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
        direct_read = self.is_2240 or self.is_2209
        if sg is not None:
            if direct_read or sg > 0:
                self.samples_sg.append(sg)
                self.samples_time.append(rel_t)
                if self._sample_cs_flag:
                    cs = self._read_cs()
                    if cs is not None:
                        self.samples_cs.append(cs)
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
            'std': statistics.pstdev(sorted_s) if n > 1 else 0.0,
            'n': n,
        }

    # ─── CSV / HTML output ──────────────────────────────────────────

    def _write_csv(self, path, results, meta, mode):
        with open(path, 'w') as f:
            f.write("# TMC Flow Test v%s results (mode: %s)\n"
                    % (MODULE_VERSION, mode))
            f.write("# Plugin by Steven (Fragmon) — Crydteam\n")
            f.write("# YouTube: https://www.youtube.com/@crydteamprinting\n")
            for k, v in meta.items():
                f.write("# %s: %s\n" % (k, v))

            # CSV header depends on mode
            if mode == 'cs':
                f.write("phase,flow_mm3s,sg_median,sg_p25,sg_p75,sg_avg,"
                        "sg_min,sg_max,sg_n,cs_median,cs_p25,cs_p75,cs_avg,"
                        "n_repeats,sg_run_cv_pct,run_sg_avgs,run_cs_avgs\n")
            else:
                f.write("phase,flow_mm3s,sg_median,sg_p25,sg_p75,sg_avg,"
                        "sg_min,sg_max,sg_n,n_repeats,sg_run_cv_pct,"
                        "run_sg_avgs\n")

            for r in results:
                sg = r.get('sg') or {}
                cs = r.get('cs') or {}
                rc = r.get('run_consistency') or {}
                run_sg = r.get('run_sg_avgs') or []
                run_cs = r.get('run_cs_avgs') or []
                phase = r.get('phase', 'coarse')

                def fmt(d, key):
                    v = d.get(key, '')
                    if isinstance(v, float):
                        return "%.1f" % v
                    return str(v)

                if mode == 'cs':
                    f.write("%s,%.2f,%s,%s,%s,%s,%s,%s,%s,"
                            "%s,%s,%s,%s,%d,%s,%s,%s\n" % (
                        phase,
                        r['flow'],
                        fmt(sg, 'median'), fmt(sg, 'p25'), fmt(sg, 'p75'),
                        fmt(sg, 'avg'), sg.get('min', ''), sg.get('max', ''),
                        sg.get('n', 0),
                        fmt(cs, 'median'), fmt(cs, 'p25'), fmt(cs, 'p75'),
                        fmt(cs, 'avg'),
                        len(run_sg),
                        "%.1f" % rc.get('sg_cv', 0) if rc else '',
                        '|'.join("%.1f" % v for v in run_sg),
                        '|'.join("%.1f" % v for v in run_cs),
                    ))
                else:
                    f.write("%s,%.2f,%s,%s,%s,%s,%s,%s,%s,"
                            "%d,%s,%s\n" % (
                        phase,
                        r['flow'],
                        fmt(sg, 'median'), fmt(sg, 'p25'), fmt(sg, 'p75'),
                        fmt(sg, 'avg'), sg.get('min', ''), sg.get('max', ''),
                        sg.get('n', 0),
                        len(run_sg),
                        "%.1f" % rc.get('sg_cv', 0) if rc else '',
                        '|'.join("%.1f" % v for v in run_sg),
                    ))

    def _write_html(self, path, results, meta, limit_reason, mode):
        """Compact HTML report with chart."""
        flows = [r['flow'] for r in results]
        phases = [r.get('phase', 'coarse') for r in results]
        sg_label = self._get_sg_label()
        sg_median = [r['sg']['median'] if r['sg'] else None for r in results]
        sg_p25 = [r['sg']['p25'] if r['sg'] else None for r in results]
        sg_p75 = [r['sg']['p75'] if r['sg'] else None for r in results]
        sg_avg = [r['sg']['avg'] if r['sg'] else None for r in results]

        cs_chart_html = ""
        cs_chart_script = ""
        if mode == 'cs':
            cs_median = [r['cs']['median'] if r.get('cs') else None
                         for r in results]
            cs_chart_html = """
<div class="chart-container">
  <h2>CS_ACTUAL vs. Flow Rate</h2>
  <p>CoolStep current scale (0-31). Higher = more current applied.
     Drops indicate motor needs less torque (or has lost load entirely).</p>
  <canvas id="csChart"></canvas>
</div>
"""
            cs_chart_script = """
const csMedian = %s;
new Chart(document.getElementById('csChart'), {
    type: 'line',
    data: { labels: flows,
        datasets: [{ label: 'CS_ACTUAL median', data: csMedian,
                     borderColor: '#d32f2f', fill: false, borderWidth: 2,
                     pointRadius: 4 }] },
    options: { ...commonOptions, scales: { ...commonOptions.scales,
        y: { title: { display: true, text: 'CS_ACTUAL (0-31)' },
             min: 0, max: 32 } } },
});
""" % json.dumps(cs_median)

        if limit_reason:
            summary_html = (
                '<div class="summary"><h2>Result</h2>'
                '<p>Stop reason: <strong>%s</strong></p></div>'
                % limit_reason)
        else:
            summary_html = (
                '<div class="summary"><h2>Result</h2>'
                '<p>Test completed without trigger.</p></div>')

        meta_html = ''.join(
            '<div><strong>%s:</strong> %s</div>' % (k, v)
            for k, v in meta.items())

        # Data table
        rows = []
        for r in results:
            sg = r.get('sg') or {}
            cs = r.get('cs') or {}
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

            if mode == 'cs':
                rows.append(
                    "<tr><td>%.1f</td><td><b>%s</b></td>"
                    "<td>%s</td><td>%s</td><td><b>%s</b></td>"
                    "<td>%d</td><td%s>%s</td></tr>" % (
                        r['flow'], fmt(sg, 'median'),
                        fmt(sg, 'p25'), fmt(sg, 'p75'),
                        fmt(cs, 'median'), sg.get('n', 0),
                        cv_class, cv_str or '-'))
            else:
                rows.append(
                    "<tr><td>%.1f</td><td><b>%s</b></td>"
                    "<td>%s</td><td>%s</td><td>%s</td>"
                    "<td>%d</td><td%s>%s</td></tr>" % (
                        r['flow'], fmt(sg, 'median'),
                        fmt(sg, 'p25'), fmt(sg, 'p75'), fmt(sg, 'avg'),
                        sg.get('n', 0), cv_class, cv_str or '-'))

        if mode == 'cs':
            table_header = (
                "<th>Flow (mm³/s)</th><th>%s median</th>"
                "<th>%s P25</th><th>%s P75</th><th>CS median</th>"
                "<th>n</th><th>Inter-run CV</th>"
                % (sg_label, sg_label, sg_label))
        else:
            table_header = (
                "<th>Flow (mm³/s)</th><th>%s median</th>"
                "<th>%s P25</th><th>%s P75</th><th>%s avg</th>"
                "<th>n</th><th>Inter-run CV</th>"
                % (sg_label, sg_label, sg_label, sg_label))

        table = ("<table><thead><tr>" + table_header
                 + "</tr></thead><tbody>"
                 + "".join(rows) + "</tbody></table>")

        html = """<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<title>TMC Flow Test (%(mode)s) - %(timestamp)s</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-annotation@3.0.1"></script>
<style>
body { font-family: system-ui, sans-serif; max-width: 1200px;
       margin: 20px auto; padding: 0 20px; color: #333; }
h1 { color: #1565c0; }
.meta { background: #f5f5f5; padding: 15px; border-radius: 8px;
        margin-bottom: 20px; display: grid;
        grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
        gap: 8px; font-size: 14px; }
.summary { background: #e3f2fd; padding: 15px; border-radius: 8px;
           margin-bottom: 20px; border-left: 4px solid #1976d2; }
.summary h2 { margin: 0 0 10px 0; color: #1976d2; }
.chart-container { background: white; padding: 20px;
                   border-radius: 8px; margin-bottom: 20px;
                   box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
.footer { text-align: center; color: #666; padding: 20px;
          font-size: 13px; }
.footer a { color: #1976d2; }
table { width: 100%%; border-collapse: collapse; font-size: 13px; }
th, td { padding: 8px 12px; border: 1px solid #ddd; text-align: right; }
th { background: #f5f5f5; }
</style></head><body>
<h1>TMC Flow Test Results — %(mode_upper)s mode</h1>
<p style="color:#666;margin-top:-10px;">
  Plugin by Steven (Fragmon) — Crydteam ·
  <a href="https://www.youtube.com/@crydteamprinting"
     target="_blank">YouTube: @crydteamprinting</a></p>

<div class="meta">%(meta_html)s</div>
%(summary_html)s

<div class="chart-container">
  <h2>%(sg_label)s vs. Flow Rate</h2>
  <p>Lower SG = higher mechanical load. Median is the robust statistic;
     IQR (P25-P75) shows sample spread.</p>
  <canvas id="sgChart"></canvas>
</div>

%(cs_chart_html)s

<div class="chart-container">
  <h2>Data Table</h2>
  %(data_table)s
</div>

<div class="footer">
  <p>Generated by <strong>TMC Flow Test</strong> v%(version)s
     at %(timestamp)s</p>
</div>

<script>
const flows = %(flows)s;
const phases = %(phases)s;
const sgMedian = %(sg_median)s;
const sgP25 = %(sg_p25)s;
const sgP75 = %(sg_p75)s;
const sgAvg = %(sg_avg)s;

// Build vertical-line annotations between phase transitions.
// xMin/xMax sit between two indices (e.g. 5.5) so the line falls in
// the gap between data points.
const phaseAnnotations = (() => {
    const labels = { coarse: 'Coarse', bisect: 'Bisection', verify: 'Verify' };
    const colors = { coarse: '#90a4ae', bisect: '#fb8c00', verify: '#43a047' };
    const ann = {};
    for (let i = 1; i < phases.length; i++) {
        if (phases[i] !== phases[i-1]) {
            const x = i - 0.5;
            ann['phase_' + i] = {
                type: 'line', xMin: x, xMax: x,
                borderColor: colors[phases[i]] || '#999',
                borderWidth: 2, borderDash: [6, 4],
                label: {
                    display: true, content: labels[phases[i]] || phases[i],
                    position: 'start', backgroundColor: colors[phases[i]] || '#999',
                    color: '#fff', font: { size: 11, weight: 'bold' },
                    padding: { top: 2, bottom: 2, left: 6, right: 6 },
                    yAdjust: -2,
                },
            };
        }
    }
    // Mark the very first phase too with a label at index 0
    if (phases.length > 0) {
        ann['phase_start'] = {
            type: 'line', xMin: -0.5, xMax: -0.5,
            borderColor: 'rgba(0,0,0,0)', borderWidth: 0,
            label: {
                display: true, content: labels[phases[0]] || phases[0],
                position: 'start',
                backgroundColor: colors[phases[0]] || '#999',
                color: '#fff', font: { size: 11, weight: 'bold' },
                padding: { top: 2, bottom: 2, left: 6, right: 6 },
                yAdjust: -2, xAdjust: 30,
            },
        };
    }
    return ann;
})();

const commonOptions = {
    responsive: true,
    interaction: { mode: 'index', intersect: false },
    scales: { x: { title: { display: true, text: 'Flow Rate (mm³/s)' } } },
    plugins: {
        legend: { position: 'top' },
        annotation: { annotations: phaseAnnotations },
    },
};

new Chart(document.getElementById('sgChart'), {
    type: 'line',
    data: { labels: flows, datasets: [
        { label: 'P75', data: sgP75,
          borderColor: 'rgba(150,200,255,0.5)',
          backgroundColor: 'rgba(150,200,255,0.15)', fill: '+1',
          borderDash: [3,3], pointRadius: 2 },
        { label: 'median', data: sgMedian, borderColor: '#1976d2',
          fill: false, borderWidth: 3, pointRadius: 5 },
        { label: 'P25', data: sgP25,
          borderColor: 'rgba(150,200,255,0.5)', fill: false,
          borderDash: [3,3], pointRadius: 2 },
        { label: 'avg', data: sgAvg, borderColor: '#90a4ae',
          borderDash: [6,3], fill: false, borderWidth: 1, pointRadius: 0 },
    ] },
    options: { ...commonOptions, scales: { ...commonOptions.scales,
        y: { title: { display: true,
             text: '%(sg_label)s (0-510)' } } } },
});
%(cs_chart_script)s
</script>
</body></html>"""
        rendered = html % {
            'timestamp': meta.get('timestamp', '-'),
            'version': MODULE_VERSION,
            'mode': mode,
            'mode_upper': mode.upper(),
            'meta_html': meta_html,
            'summary_html': summary_html,
            'sg_label': sg_label,
            'data_table': table,
            'cs_chart_html': cs_chart_html,
            'cs_chart_script': cs_chart_script,
            'flows': json.dumps(flows),
            'phases': json.dumps(phases),
            'sg_median': json.dumps(sg_median),
            'sg_p25': json.dumps(sg_p25),
            'sg_p75': json.dumps(sg_p75),
            'sg_avg': json.dumps(sg_avg),
        }
        with open(path, 'w') as f:
            f.write(rendered)

    def _save_report(self, results, meta, timestamp, limit_reason,
                     no_html, mode, gcmd=None, announce=True):
        """Save CSV and HTML report. Safe to call repeatedly."""
        if not results:
            return
        try:
            os.makedirs(self.output_dir, exist_ok=True)
            csv_path = os.path.join(
                self.output_dir, 'tmc_flow_%s_%s.csv' % (mode, timestamp))
            self._write_csv(csv_path, results, meta, mode)
            if announce and gcmd is not None:
                gcmd.respond_info("CSV saved: %s" % csv_path)
            if not no_html:
                html_path = os.path.join(
                    self.output_dir,
                    'tmc_flow_%s_%s.html' % (mode, timestamp))
                self._write_html(html_path, results, meta, limit_reason, mode)
                if announce and gcmd is not None:
                    gcmd.respond_info("HTML saved: %s" % html_path)
        except Exception as e:
            if gcmd is not None:
                gcmd.respond_info(
                    "Warning: report write failed: %s" % e)
            logging.exception("tmc_flow_test: report write failed")

    # ─── Single flow step measurement ───────────────────────────────

    def _measure_step(self, gcmd, target_flow, step_duration, repeat,
                      sample_cs=False, skip_warmup=True):
        """Run a single flow measurement (multiple repetitions, aggregate)."""
        mm_per_sec = target_flow / self.filament_area
        feed_rate = mm_per_sec * 60.0
        extrude_length = mm_per_sec * step_duration

        per_run_sg = []
        per_run_cs = []
        run_sg_avgs = []
        run_cs_avgs = []

        for rep in range(repeat):
            self._start_sampling(sample_cs=sample_cs)
            try:
                self.gcode.run_script_from_command(
                    "G1 E%.4f F%.1f\nM400" % (extrude_length, feed_rate))
            finally:
                self._stop_sampling()

            run_sg = list(self.samples_sg)
            per_run_sg.append(run_sg)
            if run_sg:
                run_sg_avgs.append(sum(run_sg) / len(run_sg))
            if sample_cs:
                run_cs = list(self.samples_cs)
                per_run_cs.append(run_cs)
                if run_cs:
                    run_cs_avgs.append(sum(run_cs) / len(run_cs))

            if rep < repeat - 1:
                self.gcode.run_script_from_command("G4 P300")

        # Warmup-skip: if first run deviates >10% from rest, exclude it
        warmup_dropped = False
        included_indices = list(range(len(run_sg_avgs)))
        if skip_warmup and len(run_sg_avgs) >= 3:
            run1 = run_sg_avgs[0]
            rest_mean = sum(run_sg_avgs[1:]) / len(run_sg_avgs[1:])
            if rest_mean > 0:
                deviation = abs(run1 - rest_mean) / rest_mean
                if deviation > 0.10:
                    warmup_dropped = True
                    included_indices = list(range(1, len(run_sg_avgs)))

        # Aggregate from included runs
        agg_sg = []
        agg_cs = []
        for idx in included_indices:
            agg_sg.extend(per_run_sg[idx])
            if sample_cs and idx < len(per_run_cs):
                agg_cs.extend(per_run_cs[idx])

        sg_stats = self._stats(agg_sg)
        cs_stats = self._stats(agg_cs) if sample_cs else None

        included_sg_avgs = [run_sg_avgs[i] for i in included_indices
                            if i < len(run_sg_avgs)]
        run_consistency = None
        if len(included_sg_avgs) > 1:
            sg_run_std = statistics.pstdev(included_sg_avgs)
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
        if sample_cs and cs_stats is not None:
            cs_med_str = "%.0f" % cs_stats['median']
            gcmd.respond_info(
                "  %.1f mm³/s | SG median = %s | CS median = %s | "
                "run-to-run CV = %s%s"
                % (target_flow, sg_med_str, cs_med_str, cv_str, warmup_str))
        else:
            gcmd.respond_info(
                "  %.1f mm³/s | SG median = %s | "
                "run-to-run CV = %s%s"
                % (target_flow, sg_med_str, cv_str, warmup_str))

        return {
            'flow': target_flow,
            'sg': sg_stats,
            'cs': cs_stats,
            'run_consistency': run_consistency,
            'run_sg_avgs': run_sg_avgs,
            'run_cs_avgs': run_cs_avgs,
        }

    # ─── Trigger detection — SG-only mode ──────────────────────────

    def _check_triggers_sg(self, results):
        """SG-only triggers: for setups with CoolStep disabled.

        Two triggers, both require SG > 50 (informative range) and only
        fire when stepping UP:
          1. SG abnormal jump: actual > 2x expected AND > +15
          2. SG plateau over 2 steps: cumulative rise < 0.5x expected
        """
        if not results or len(results) < 5:
            return None
        r = results[-1]
        sg_stats = r['sg']
        if sg_stats is None or sg_stats['n'] == 0:
            return None
        sg_med = sg_stats['median']
        target_flow = r['flow']

        if sg_med <= SG_MIN_INFORMATIVE:
            return None

        prev_flow = results[-2].get('flow', 0)
        if target_flow < prev_flow - 0.001:
            return None  # bisection going down: don't trigger

        sg_deltas = []
        for j in range(max(1, len(results) - 5), len(results) - 1):
            rj, rj_prev = results[j], results[j-1]
            if rj.get('sg') and rj_prev.get('sg'):
                sg_deltas.append(
                    rj['sg']['median'] - rj_prev['sg']['median'])

        if len(sg_deltas) < 3:
            return None

        expected_delta = sum(sg_deltas) / len(sg_deltas)
        actual_delta = sg_med - results[-2]['sg']['median']

        sg_label = self._get_sg_label()

        # Trigger 1: SG abnormal jump
        if (expected_delta > 0
                and actual_delta > expected_delta * 2.0
                and actual_delta > 15):
            return ("%s abnormal jump: +%.0f vs expected +%.0f "
                    "(%.1fx larger) — slip detected"
                    % (sg_label, actual_delta, expected_delta,
                       actual_delta / expected_delta))

        # Trigger 2: SG plateau over 2 steps
        if expected_delta > 5:
            prev_actual = (results[-2]['sg']['median']
                           - results[-3]['sg']['median'])
            cumulative_2step = actual_delta + prev_actual
            expected_2step = expected_delta * 2
            if cumulative_2step < expected_2step * 0.5:
                return ("%s plateau over 2 steps: rose only +%.0f "
                        "vs typical +%.0f — flow no longer increasing "
                        "motor load (slip starting)"
                        % (sg_label, cumulative_2step, expected_2step))

        return None

    # ─── Trigger detection — CoolStep + SG mode ────────────────────

    def _check_triggers_cs(self, results, baseline_cs):
        """CoolStep + SG triggers (Mode A + B fallback).

        AUTO-DETECTS whether CoolStep is actually changing CS_ACTUAL.
        If CS is essentially static (range < 1.0), falls back to SG-only.

        CS-based triggers (when CoolStep is active):
          A1. CS jump UP >=+5: sudden load → approaching slip
          A2. CS leaves regulation (CS<deep_reg → CS>=deep_reg)
          A3. CS drop >5 (hard stall: motor lost load)
        SG fallback (always):
          B1. SG abnormal jump (>2x expected, >+15)

        deep_reg = baseline_cs + 5 (dynamic, depends on hardware)
        """
        if not results or len(results) < 3:
            return None
        r = results[-1]
        sg_stats = r['sg']
        cs_stats = r['cs']
        target_flow = r['flow']

        if cs_stats is None or cs_stats['n'] == 0:
            return None

        prev_cs = results[-2].get('cs')
        prev2_cs = results[-3].get('cs')
        if not prev_cs or not prev2_cs:
            return None

        # Detect mode: is CoolStep actually varying?
        cs_meds = [r_['cs']['median'] for r_ in results
                   if r_.get('cs') and r_['cs'].get('median') is not None]
        cs_range = max(cs_meds) - min(cs_meds) if len(cs_meds) >= 2 else 0
        coolstep_active = cs_range > 1.0

        prev_flow = results[-2].get('flow', 0)
        going_up_or_same = target_flow >= prev_flow - 0.001

        cs_med = cs_stats['median']
        sg_med = sg_stats['median'] if sg_stats and sg_stats['n'] > 0 else None
        sg_label = self._get_sg_label()

        # ─── CoolStep-based triggers ───
        if coolstep_active:
            if baseline_cs is not None and baseline_cs > 0:
                CS_DEEP_REG = baseline_cs + 5.0
                cs_elevated = baseline_cs + 3.0
            else:
                CS_DEEP_REG = 22.0
                cs_elevated = 17.0

            cs_step_change = cs_med - prev_cs['median']

            # A1: CS jump UP >=+5
            if (going_up_or_same
                    and prev_cs['median'] < CS_DEEP_REG
                    and cs_step_change >= 5.0):
                return ("CS_ACTUAL jumped +%.1f in one step "
                        "(median %.1f → %.1f) — sudden load increase, "
                        "motor approaching limit"
                        % (cs_step_change, prev_cs['median'], cs_med))

            # A2: CS leaves regulation
            if (going_up_or_same
                    and prev_cs['median'] < CS_DEEP_REG
                    and cs_med >= CS_DEEP_REG
                    and cs_step_change >= 3.0):
                return ("CS_ACTUAL left deep regulation: %.1f → %.1f "
                        "(threshold %.0f) — motor approaching limit"
                        % (prev_cs['median'], cs_med, CS_DEEP_REG))

            # A3: CS hard drop
            if (going_up_or_same
                    and prev_cs['median'] > cs_elevated
                    and cs_step_change < -5.0):
                had_regulation = any(
                    r_.get('cs') and r_['cs']['median'] < CS_DEEP_REG
                    for r_ in results[:-1])
                if had_regulation:
                    return ("CS_ACTUAL dropped %.1f in one step "
                            "(median %.1f → %.1f) — hard stall: "
                            "motor lost load contact"
                            % (-cs_step_change, prev_cs['median'], cs_med))

        # ─── SG fallback / backup ───
        if (going_up_or_same
                and sg_med is not None
                and sg_med > SG_MIN_INFORMATIVE
                and len(results) >= 4):
            sg_deltas = []
            for j in range(max(1, len(results) - 5), len(results) - 1):
                rj, rj_prev = results[j], results[j-1]
                if not (rj.get('sg') and rj_prev.get('sg')
                        and rj.get('cs') and rj_prev.get('cs')):
                    continue
                if coolstep_active:
                    deep_reg = (baseline_cs + 5.0
                                if baseline_cs is not None
                                and baseline_cs > 0 else 22.0)
                    if (rj['cs']['median'] >= deep_reg
                            or rj_prev['cs']['median'] >= deep_reg):
                        continue
                sg_deltas.append(
                    rj['sg']['median'] - rj_prev['sg']['median'])

            if len(sg_deltas) >= 3:
                expected_delta = sum(sg_deltas) / len(sg_deltas)
                actual_delta = (sg_med - results[-2]['sg']['median'])
                if (expected_delta > 0
                        and actual_delta > expected_delta * 2.0
                        and actual_delta > 15):
                    return ("%s abnormal jump: +%.0f vs expected +%.0f "
                            "(%.1fx larger) — slip detected"
                            % (sg_label, actual_delta, expected_delta,
                               actual_delta / expected_delta))

        return None

    # ─── Helper: rotation_distance lookup ───────────────────────────

    def _get_rotation_distance(self, extruder):
        try:
            rd = extruder.extruder_stepper.stepper.get_rotation_distance()
            return rd[0] if isinstance(rd, tuple) else rd
        except AttributeError:
            pass
        try:
            rd = extruder.stepper.get_rotation_distance()
            return rd[0] if isinstance(rd, tuple) else rd
        except AttributeError:
            pass
        try:
            cfg = self.printer.lookup_object('configfile')
            settings = cfg.get_status(
                self.reactor.monotonic())['settings']
            return float(settings[self.stepper_name]['rotation_distance'])
        except Exception:
            return None

    # ─── TMC_FLOW_STATUS — diagnostic ───────────────────────────────

    def cmd_TMC_FLOW_STATUS(self, gcmd):
        """Show current TMC SG/CS values for the extruder."""
        self._lookup_tmc()
        activate = gcmd.get_int('ACTIVATE', 1, minval=0, maxval=1)

        if activate:
            gcmd.respond_info("Activating motor (1 mm extrusion)...")
            self.gcode.run_script_from_command(
                "M83\nG1 E1 F60\nM400\nG92 E0")

        gcmd.respond_info(
            "===== TMC %s Status (stepper '%s') ====="
            % (self.driver_type or 'unknown', self.stepper_name))

        sg = self._read_sg()
        cs = self._read_cs()
        sg_label = self._get_sg_label()
        gcmd.respond_info(
            "%s: %s (range 0-510, lower = more load)"
            % (sg_label, str(sg) if sg is not None else 'n/a'))
        gcmd.respond_info(
            "CS_ACTUAL: %s (CoolStep current scale, 0-31)"
            % (str(cs) if cs is not None else 'n/a'))

        # Detect which mode the user has configured for
        semin = self._read_cs_semin()
        if semin == 0:
            preferred_mode = 'sg'
            gcmd.respond_info(
                "→ Detected configuration: CoolStep DISABLED "
                "(driver_SEMIN=0)\n"
                "  Run: TMC_FLOW_FIND_MAX  "
                "(auto-selects SG-only mode for this config)")
        else:
            preferred_mode = 'cs'
            gcmd.respond_info(
                "→ Detected configuration: CoolStep ENABLED "
                "(driver_SEMIN=%d)\n"
                "  Run: TMC_FLOW_FIND_MAX  "
                "(auto-selects CoolStep mode for this config)" % semin)

        # Run check for the preferred mode and show results
        problems, infos = self._check_tmc_config(preferred_mode)
        if problems:
            gcmd.respond_info(
                "Configuration issues found for %s mode:"
                % preferred_mode.upper())
            for fname, val, _desc in problems:
                gcmd.respond_info("  ⚠ %s = %s" % (fname, val))
        else:
            gcmd.respond_info(
                "✓ Configuration looks good for %s mode."
                % preferred_mode.upper())
        for info in infos:
            gcmd.respond_info(info)

    def _read_cs_semin(self):
        """Helper: read semin to determine current mode. 0 if unknown."""
        try:
            val = self.tmc.fields.get_field('semin')
            return val if val is not None else 0
        except (KeyError, AttributeError):
            return 0

    # ─── Main commands ──────────────────────────────────────────────

    def cmd_TMC_FLOW_FIND_MAX(self, gcmd):
        """Auto-detect mode from driver_SEMIN, then run flow test.

        Optional MODE parameter to override:
          MODE=auto  (default) — detect from driver_SEMIN
          MODE=sg              — force SG-only mode
          MODE=cs              — force CoolStep + SG mode
        """
        self._lookup_tmc()

        mode_param = gcmd.get('MODE', 'auto').lower()

        if mode_param == 'auto':
            # Auto-detect from driver_SEMIN
            try:
                semin = self.tmc.fields.get_field('semin')
                if semin is None:
                    semin = 0
            except (KeyError, AttributeError):
                raise gcmd.error(
                    "Could not read driver_SEMIN to auto-detect mode. "
                    "Please specify MODE=sg or MODE=cs explicitly.")

            if semin == 0:
                mode = 'sg'
                gcmd.respond_info(
                    "Auto-detect: driver_SEMIN = 0 → CoolStep is disabled. "
                    "Using SG-only mode.")
            else:
                mode = 'cs'
                gcmd.respond_info(
                    "Auto-detect: driver_SEMIN = %d → CoolStep is enabled. "
                    "Using CoolStep + SG mode." % semin)
        elif mode_param == 'sg':
            mode = 'sg'
            gcmd.respond_info("Mode forced via MODE=sg parameter.")
        elif mode_param == 'cs':
            mode = 'cs'
            gcmd.respond_info("Mode forced via MODE=cs parameter.")
        else:
            raise gcmd.error(
                "Invalid MODE parameter: '%s'. Use 'auto', 'sg', or 'cs'."
                % mode_param)

        self._mode = mode
        self._run_find_max(gcmd, mode=mode)

    def cmd_TMC_FLOW_FIND_MAX_SG(self, gcmd):
        """Force SG-only mode — for setups with CoolStep disabled.

        Equivalent to: TMC_FLOW_FIND_MAX MODE=sg
        """
        self._mode = 'sg'
        self._run_find_max(gcmd, mode='sg')

    def cmd_TMC_FLOW_FIND_MAX_CS(self, gcmd):
        """Force CoolStep + SG mode — for setups with CoolStep enabled.

        Equivalent to: TMC_FLOW_FIND_MAX MODE=cs
        """
        self._mode = 'cs'
        self._run_find_max(gcmd, mode='cs')

    def _run_find_max(self, gcmd, mode):
        """Common bracket-bisection algorithm. Trigger logic switches
        based on mode."""
        self._lookup_tmc()

        start_flow = gcmd.get_float('START', 10.0, above=0.)
        max_flow = gcmd.get_float('MAX', 80.0, above=start_flow)
        coarse_step = gcmd.get_float('COARSE_STEP', 10.0, above=0.)
        min_step = gcmd.get_float('MIN_STEP', 1.0, above=0.)
        step_duration = gcmd.get_float('DURATION', 5.0, above=0.5)
        repeat = gcmd.get_int('REPEAT', 5, minval=1, maxval=10)
        verify_repeats = gcmd.get_int(
            'VERIFY_REPEATS', 5, minval=1, maxval=10)
        purge = gcmd.get_float('PURGE', 0.0, minval=0.)
        cooldown = gcmd.get_float('COOLDOWN', 60.0, minval=0., maxval=300.)
        max_bisect = gcmd.get_int(
            'MAX_BISECT_STEPS', 6, minval=2, maxval=15)
        no_html = gcmd.get_int('NO_HTML', 0, minval=0, maxval=1)
        skip_check = gcmd.get_int(
            'SKIP_TMC_CHECK', 0, minval=0, maxval=1)

        if min_step >= coarse_step:
            raise gcmd.error(
                "MIN_STEP (%.1f) must be smaller than COARSE_STEP (%.1f)"
                % (min_step, coarse_step))

        # ─── TMC config check ───
        if not skip_check:
            problems, infos = self._check_tmc_config(mode)
            if problems:
                msg = ("\n=== TMC Configuration Issue(s) for %s mode ===\n"
                       % mode.upper())
                for fname, val, desc in problems:
                    msg += "Problem: %s (current value: %s)\n%s\n\n" % (
                        fname, val, desc)
                msg += ("After updating printer.cfg, run:\n"
                        "  FIRMWARE_RESTART\n"
                        "  TMC_FLOW_FIND_MAX\n\n"
                        "To skip this check (advanced):\n"
                        "  TMC_FLOW_FIND_MAX SKIP_TMC_CHECK=1")
                raise gcmd.error(msg)
            gcmd.respond_info(
                "TMC configuration check passed for %s mode."
                % mode.upper())
            for info in infos:
                gcmd.respond_info(info)

        # ─── Hotend temp check ───
        extruder = self.printer.lookup_object('extruder')
        heater = extruder.get_heater()
        cur_temp, _ = heater.get_temp(self.reactor.monotonic())
        target_temp = heater.target_temp
        if cur_temp < self.min_hotend_temp:
            raise gcmd.error(
                "Hotend too cold: %.1f°C (min %.1f°C)."
                % (cur_temp, self.min_hotend_temp))
        if target_temp > 0 and cur_temp < target_temp - 5.0:
            raise gcmd.error(
                "Hotend not at target: %.1f°C (target %.1f°C). "
                "Wait for M109 or run M109 S%d before testing."
                % (cur_temp, target_temp, int(target_temp)))

        rotation_distance = self._get_rotation_distance(extruder)
        if rotation_distance is None:
            raise gcmd.error("Could not determine rotation_distance")

        timestamp = time.strftime('%Y-%m-%d_%H-%M-%S')
        sg_label = self._get_sg_label()
        meta = {
            'timestamp': timestamp,
            'mode': mode.upper(),
            'driver': '%s on %s (%s register, sample rate %.0f Hz)' % (
                self.driver_type, self.stepper_name, sg_label,
                1.0 / SAMPLE_INTERVAL),
            'algorithm': 'ADAPTIVE BISECTION (coarse=%.1f, min=%.1f, '
                         'repeats=%d)' % (coarse_step, min_step, repeat),
            'hotend_temp': '%.1f °C (target %.1f °C)' % (
                cur_temp, target_temp),
            'filament_diameter': '%.2f mm' % self.filament_diameter,
            'melt_zone_length': '%.1f mm' % self.melt_zone_length,
            'rotation_distance': '%.4f mm' % rotation_distance,
            'flow_range': '%.1f → %.1f mm³/s (adaptive)' % (
                start_flow, max_flow),
            'step_duration': '%.1f s' % step_duration,
        }

        # ─── Banner ───
        mode_desc = ("SG-only mode (CoolStep disabled, "
                     "SG abnormal jump + plateau triggers)" if mode == 'sg'
                     else "CoolStep + SG mode (CoolStep enabled, "
                     "CS jump/leave/drop + SG backup triggers)")
        gcmd.respond_info(
            "===== TMC Flow Test v%s — %s mode =====\n"
            "%s\n"
            "Range: %.0f → %.0f mm³/s | Coarse: %.0f | Min: %.0f mm³/s\n"
            "Each measurement: %d reps × %.1f s (median over all samples)\n"
            "Cool-down between phases: %.0f s | Hotend: %.1f°C "
            "(target %.1f°C)\n"
            "Sample rate: %.0f Hz | Driver: %s | Register: %s\n"
            "------------------------------------------------\n"
            "Algorithm:\n"
            "  Phase 1 (Coarse): increase flow in big steps until trigger\n"
            "  Phase 2 (Bisection): narrow to ±%.0f mm³/s by halving\n"
            "  Phase 3 (Verification): confirm with %d reps\n"
            "================================================"
            % (MODULE_VERSION, mode.upper(), mode_desc,
               start_flow, max_flow, coarse_step, min_step,
               repeat, step_duration, cooldown,
               cur_temp, target_temp,
               1.0 / SAMPLE_INTERVAL, self.driver_type, sg_label,
               min_step, verify_repeats))

        if purge > 0:
            self.gcode.run_script_from_command(
                "M83\nG1 E%.2f F300\nG92 E0" % purge)
        self.gcode.run_script_from_command("M83\nG92 E0")

        results = []
        baseline_cs = None
        sample_cs = (mode == 'cs')

        def measure_and_save(flow, phase):
            r = self._measure_step(
                gcmd, flow, step_duration, repeat,
                sample_cs=sample_cs)
            r['phase'] = phase
            results.append(r)
            self._save_report(
                results, meta, timestamp, None, no_html, mode,
                gcmd=None, announce=False)
            return r

        def check(results):
            """Dispatch to right trigger function based on mode."""
            if mode == 'sg':
                return self._check_triggers_sg(results)
            return self._check_triggers_cs(results, baseline_cs)

        # ─── PHASE 1: Coarse ───
        gcmd.respond_info(
            "\n>>> Phase 1: Coarse Upward Sweep <<<\n"
            "  Stepping up by %.0f mm³/s. Each step: %d reps × %.1f s."
            % (coarse_step, repeat, step_duration))

        flow = start_flow
        low = None
        high = None
        first_trigger_reason = None

        while flow <= max_flow + 0.001:
            r = measure_and_save(flow, 'coarse')

            # Track baseline CS for CS-mode trigger thresholds
            if (mode == 'cs' and baseline_cs is None
                    and r.get('cs') and r['cs']['n'] > 0):
                cs_med = r['cs']['median']
                if cs_med < 30:  # not in saturation
                    baseline_cs = cs_med

            reason = check(results)
            if reason:
                high = flow
                low = flow - coarse_step
                first_trigger_reason = reason
                gcmd.respond_info(
                    "  >>> TRIGGER at %.1f mm³/s — %s\n"
                    "      → Safe range narrowed to [%.1f, %.1f] mm³/s"
                    % (flow, reason, low, high))
                break

            low = flow
            self.gcode.run_script_from_command("G4 P500")
            flow += coarse_step

        if high is None:
            gcmd.respond_info(
                "Reached MAX %.1f mm³/s without trigger. Try MAX higher."
                % max_flow)
            self._save_report(results, meta, timestamp, None,
                              no_html, mode, gcmd=gcmd)
            return

        if low < start_flow:
            gcmd.respond_info(
                "Trigger fired on first step (%.1f) — lower START." % high)
            self._save_report(results, meta, timestamp, first_trigger_reason,
                              no_html, mode, gcmd=gcmd)
            return

        # ─── PHASE 2: Bisection ───
        if cooldown > 0:
            gcmd.respond_info(
                "  ... Cool-down: %.0f s ..." % cooldown)
            self.gcode.run_script_from_command(
                "G4 P%d" % int(cooldown * 1000))

        gcmd.respond_info(
            "\n>>> Phase 2: Bisection <<<\n"
            "  Narrowing [%.0f, %.0f] by halving until interval ≤ %.0f. "
            "Up to %d steps."
            % (low, high, min_step, max_bisect))

        bisect_iter = 0
        last_trigger_reason = first_trigger_reason
        while (high - low) > min_step + 0.001 and bisect_iter < max_bisect:
            bisect_iter += 1
            raw_mid = (low + high) / 2.0
            mid = round(raw_mid / min_step) * min_step
            if mid <= low + 0.001 or mid >= high - 0.001:
                break

            r = measure_and_save(mid, 'bisect')
            reason = check(results)
            if reason:
                high = mid
                last_trigger_reason = reason
                gcmd.respond_info(
                    "  >>> TRIGGER at %.1f — %s\n"
                    "      → [%.1f, %.1f] (%d/%d)"
                    % (mid, reason, low, high, bisect_iter, max_bisect))
            else:
                low = mid
                gcmd.respond_info(
                    "  >>> %.1f mm³/s SAFE → [%.1f, %.1f] (%d/%d)"
                    % (mid, low, high, bisect_iter, max_bisect))
            self.gcode.run_script_from_command("G4 P500")

        # ─── PHASE 3: Verify ───
        if cooldown > 0:
            gcmd.respond_info(
                "  ... Cool-down before verification: %.0f s ..." % cooldown)
            self.gcode.run_script_from_command(
                "G4 P%d" % int(cooldown * 1000))

        gcmd.respond_info(
            "\n>>> Phase 3: Verification at %.1f mm³/s <<<\n"
            "  Confirming with %d repetitions."
            % (low, verify_repeats))
        verify_result = self._measure_step(
            gcmd, low, step_duration, verify_repeats,
            sample_cs=sample_cs)
        verify_result['phase'] = 'verify'
        results.append(verify_result)

        verify_cv = (verify_result.get('run_consistency', {}).get('sg_cv', 0)
                     if verify_result.get('run_consistency') else 0)

        max_safe = low
        if verify_cv < 5:
            quality = "excellent (very stable)"
        elif verify_cv < 10:
            quality = "good (stable)"
        elif verify_cv < 20:
            quality = "acceptable (some variation)"
        else:
            quality = "poor (high variation — re-run advised)"

        gcmd.respond_info(
            "\n========== FINAL RESULT ==========\n"
            "Test mode: %s\n"
            "Maximum safe volumetric flow: %.1f mm³/s\n"
            "Verification quality: %s (CV = %.1f%%)\n"
            "----------------------------------\n"
            "Slicer recommendation:\n"
            "  Conservative (80%%): %.1f mm³/s   ← recommended\n"
            "  Aggressive (90%%):   %.1f mm³/s   ← only with margin\n"
            "----------------------------------\n"
            "Detailed data: see CSV/HTML report\n"
            "=================================="
            % (mode.upper(), max_safe, quality, verify_cv,
               max_safe * 0.8, max_safe * 0.9))

        self._save_report(results, meta, timestamp, last_trigger_reason,
                          no_html, mode, gcmd=gcmd)
        self.gcode.run_script_from_command("G92 E0")


def load_config(config):
    return TMCFlowTest(config)
