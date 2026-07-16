# TMC Flow Test — this repo has moved 📦

**The plugin now lives in the [Crydteam Tools](https://github.com/Fragmon/crydteamtools) collection** (folder [`max_flow_test/`](https://github.com/Fragmon/crydteamtools/tree/main/max_flow_test)), together with the Speed Test plugin and future Crydteam tools.

All development, issues and releases continue there. This repository stays
only as a pointer and receives **no further updates**.

Plugin by **Steven (Fragmon) — Crydteam** ·
[![YouTube](https://img.shields.io/badge/YouTube-@crydteamprinting-red?logo=youtube)](https://www.youtube.com/@crydteamprinting)

---

## Fresh install

```bash
cd ~
git clone https://github.com/Fragmon/crydteamtools.git
cd crydteamtools
./install.sh max_flow_test
```

Then follow the plugin README:
**[crydteamtools/max_flow_test](https://github.com/Fragmon/crydteamtools/tree/main/max_flow_test)**

---

## Migrating an existing install (from this repo)

Your `printer.cfg` stays untouched — the `[tmc_flow_test]` section and the
`[include tmc_flow_test_macros.cfg]` line keep working unchanged.

**1. Install from the new repo** (this replaces the old symlinks
automatically):

```bash
cd ~
git clone https://github.com/Fragmon/crydteamtools.git
cd crydteamtools
./install.sh max_flow_test
```

**2. Remove the old update-manager entry** from
`~/printer_data/config/moonraker.conf` — delete this block (the installer
added a new `[update_manager crydteamtools]` entry instead):

```ini
[update_manager Crydteam-Tuning-Plugins]
...
```

**3. Delete the old repo folder:**

```bash
rm -rf ~/klipper_max_flow_test
```

**4. Restart:**

```bash
sudo systemctl restart moonraker
```

then `FIRMWARE_RESTART` in the printer console. Updates now appear in
Mainsail/Fluidd's update manager as **crydteamtools**.

---

Released under the GNU General Public License v3.0.
