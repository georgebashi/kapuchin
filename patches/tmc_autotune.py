# Kapuchin plugin: tmc_autotune
#
# Autotunes Trinamic (TMC) driver parameters without modifying core code.
# Ports and adapts the logic from mszturc-10/11 patches into a Kapuchin plugin:
#   - Computes PWM frequency setting, spreadCycle timing (TOFF/TBL/TPFD),
#     hysteresis window (HSTRT/HEND), and PWM parameters (PWM_GRAD/PWM_OFS).
#   - Computes speed thresholds from motor and driver characteristics and sets:
#       * THIGH = 1.2 * vmaxpwm
#       * TPWMTHRS = 0xFFFFF
#   - Avoids sensorless-homing and homing-current behavior changes.
#   - Honors user overrides: any driver_* option present in the TMC section
#     takes precedence; autotune uses configured values in its calculations.
#
# Activation (per stepper driver section):
#   - motor: value from the database (e.g. "ldo-42sth48-2804ah")
#   - voltage: supply voltage in Volts
#   - sense resistor source, in priority order:
#       1) the driver's own current_helper.sense_resistor if present
#       2) sense_resistor: <ohms> in this section
#       3) stepstick_type: from the lookup table below
#   Autotune runs on connect and after SET_TMC_CURRENT changes run current.
#
# Configuration examples (in printer.cfg):
#   [kapuchin]
#
#   [patch tmc_autotune]
#
#   [tmc5160 stepper_x]
#   motor: ldo-42sth48-2804ah
#   voltage: 24.0
#   # Optional:
#   # sense_resistor: 0.075
#   # stepstick_type: BTT_EZ_5160_PRO
#   # extra_hysteresis: 0
#   # current_scale: 0              # if you want to force CS instead of derived
#   # pwm_freq_target: 55000        # Hz; default depends on driver type
#   # driver_TOFF: <1..15>          # user override (autotune uses if set)
#   # driver_TBL:  <0..3>           # user override (autotune uses if set/valid)
#   # driver_TPFD: <0..15>          # user override (autotune uses if set/valid)
#   # driver_HSTRT/HEND:            # user override (autotune uses if set)
#   # stealthchop_threshold: <vel>  # if set, we do not touch TPWMTHRS
#   # high_velocity_threshold: <vel># if set, we do not touch THIGH
#
# Status:
#   Exposes a 'kap_tmc_autotune' status entry listing tuned steppers and the
#   last computed core parameters (per stepper).
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import os
import math
import logging
from ..extras import kapuchin_monkey as monkey

from ..extras import tmc as _tmc
from ..extras.kapuchin import bootstrap_plugin, call_original


# Default target PWM switching frequencies (Hz) per driver
PWM_FREQ_TARGETS = {
    "tmc2130": 55000.0,
    "tmc2208": 55000.0,
    "tmc2209": 55000.0,
    "tmc2240": 20000.0,  # 2240s run hotter at higher pwm_freq
    "tmc2660": 55000.0,
    "tmc5160": 55000.0,
}

# Stepstick lookup table from mszturc-7.patch
# Maps stepstick_type -> (sense_resistor_ohm, safe_max_current_A)
STEPSTICK_DEFS = {
    "REFERENCE_WOTT": (0.11, 1.2),
    "REFERENCE_2209": (0.11, 2),
    "REFERENCE_5160": (0.075, 3),
    "KRAKEN_2160_8A": (0.022, 8),
    "KRAKEN_2160_3A": (0.075, 3),
    "BTT_2240": (0.11, 2.1),
    "BTT_EZ_5160_PRO": (0.075, 2.5),
    "BTT_EZ_5160_RGB": (0.05, 4.7),
    "BTT_EZ_6609": (0.11, 2),
    "BTT_5160T": (0.022, 10.6),
    "WOTT_2209": (0.11, 1.7),
    "COREVUS_2209": (0.1, 3),
    "COREVUS_2160_OLD": (0.03, 3),
    "COREVUS_2160_5A": (0.03, 5),
    "COREVUS_2160": (0.05, 3),
    "FYSETC_2225": (0.11, 1.4),
    "FYSETC_5161": (0.06, 3.5),
    "MKS_2226": (0.17, 2.5),
    "MELLOW_FLY_5160": (0.11, 3),
    "MELLOW_FLY_HV_5160_Pro": (0.033, 6),
}

# Global caches
_DB_CACHE = None  # motor_database cfg parsed cache
_STATUS = {}      # per-stepper last computed params and activation


def _load_motor_database(path):
    """
    Very small ini-like parser for motor_database.cfg that only cares about
    [motor_constants NAME] sections and the keys we need.

    Returns: dict(name -> dict with R, L, T, S, I)
    """
    global _DB_CACHE
    if _DB_CACHE is not None:
        return _DB_CACHE

    if not os.path.exists(path):
        logging.warning("tmc_autotune: motor database file not found at %s", path)
        _DB_CACHE = {}
        return _DB_CACHE

    def _strip_comment(line):
        pos = line.find("#")
        return line if pos < 0 else line[:pos]

    db = {}
    cur = None
    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
        for raw in fh:
            line = _strip_comment(raw).strip()
            if not line:
                continue
            if line.startswith("[") and line.endswith("]"):
                sect = line[1:-1].strip()
                if sect.lower().startswith("motor_constants "):
                    name = sect.split(" ", 1)[1].strip()
                    cur = {"name": name}
                    db[name] = cur
                else:
                    cur = None
                continue
            if cur is None:
                continue
            # key: value
            if ":" in line:
                k, v = line.split(":", 1)
                key = k.strip().lower()
                val = v.strip()
                try:
                    if key == "resistance":
                        cur["R"] = float(val)
                    elif key == "inductance":
                        cur["L"] = float(val)
                    elif key == "holding_torque":
                        cur["T"] = float(val)
                    elif key == "max_current":
                        cur["I"] = float(val)
                    elif key == "steps_per_revolution":
                        cur["S"] = int(val)
                except Exception:
                    # Ignore malformed entry
                    pass
    _DB_CACHE = db
    return _DB_CACHE


class _MotorConstants(object):
    """
    Port of the math functions from mszturc-10.patch motor_constants.py
    Only the formulas used by autotune are implemented.
    """

    def __init__(self, name, spec):
        # spec: dict with keys R, L, T, I, S
        self.name = name
        self.R = float(spec.get("R", 0.0))
        self.L = float(spec.get("L", 0.0))
        self.T = float(spec.get("T", 0.0))
        self.S = int(spec.get("S", 200) or 200)
        self.I = float(spec.get("I", 0.0))
        # Back-EMF coefficient
        self.cbemf = self.T / (2.0 * max(self.I, 1e-9)) if self.I > 0 else 0.0

    def pwmgrad(self, fclk=12.5e6, steps=0, volts=24.0):
        # return int(ceil(cbemf * 2*pi * fclk * 1.46 / (volts * 256 * steps)))
        S = steps or self.S or 200
        if volts <= 0 or S <= 0:
            return 0
        return int(math.ceil(self.cbemf * 2.0 * math.pi * fclk * 1.46 / (volts * 256.0 * S)))

    def pwmofs(self, volts=24.0, current=0.0):
        I = current if (current and current > 0.0) else self.I
        if volts <= 0:
            return 0
        return int(math.ceil(374.0 * self.R * I / volts))

    def maxpwmrps(self, fclk=12.5e6, steps=0, volts=24.0, current=0.0):
        # (255 - pwmofs) / (pi * pwmgrad)
        S = steps or self.S or 200
        pg = self.pwmgrad(fclk=fclk, steps=S, volts=volts)
        po = self.pwmofs(volts=volts, current=current)
        denom = (math.pi * max(pg, 1))
        return max(0.0, (255.0 - min(po, 255)) / denom)

    def hysteresis(self, name, extra, fclk, volts, current, tbl, toff, rsense, scale):
        # Implements the verbose logging math from mszturc-10, but returning the values
        # extra is user extra hysteresis (0..8)
        I = current * math.sqrt(2.0) if (current and current > 0.0) else self.I
        if I <= 0.0 or self.L <= 0.0 or fclk <= 0.0:
            # return a conservative window
            hstrt = 3
            hend = 6
            return max(min(hstrt, 8), 1) - 1, min(hend, 12)
        try:
            tblank = 16.0 * (1.5 ** int(tbl)) / fclk
        except Exception:
            tblank = 16.0 * (1.5 ** 1) / fclk
        tsd = (12.0 + 32.0 * max(int(toff), 1)) / fclk
        dcoilblank = volts * tblank / self.L
        dcoilsd = self.R * I * 2.0 * tsd / self.L
        if scale and scale > 0:
            cs = int(scale)
        else:
            # derive cs from rsense and target I RMS peak
            try:
                cs = max(0, min(31, int(math.ceil(rsense * 32.0 * I / 0.32) - 1)))
            except Exception:
                cs = 16
        hyst = int(extra) + int(max(0.5 + ((dcoilblank + dcoilsd) * 2.0 * 248.0 * (cs + 1) / max(I, 1e-9)) / 32.0 - 8.0, -2.0))
        hstrt = max(min(hyst, 8), 1)
        hend = min(hyst - hstrt, 12)
        return hstrt - 1, hend + 3


def _resolve_rsense_and_max_current(current_helper, cfg_sense, stepstick_type):
    # Prefer current_helper.sense_resistor if present
    rsense = getattr(current_helper, "sense_resistor", None)
    max_cur = None
    if rsense is None and cfg_sense is not None:
        rsense = cfg_sense
    if stepstick_type:
        st = STEPSTICK_DEFS.get(stepstick_type, (None, None))
        # Only fallback to stepstick if neither helper nor explicit sense provided
        if rsense is None and st[0] is not None:
            rsense = st[0]
        max_cur = st[1]
    return rsense, max_cur


def _has_user_driver_override(config, field_name_upper):
    # FieldHelper.set_config_field uses "driver_FIELDNAME" convention with uppercase field
    key = f"driver_{field_name_upper}"
    try:
        # If the key exists in config (even without value), get() returns something not None
        val = config.get(key, None, False)
        return val is not None
    except Exception:
        return False


def _get_config_optional(config, getter, name, default=None, **kwargs):
    try:
        return getattr(config, getter)(name, default, **kwargs)
    except Exception:
        return default


def _lookup_field_and_set(self, field_name, field_value):
    """
    Utility bound to TMCCommandHelper instances:
    - If field exists, set via FieldHelper and push the register immediately.
    """
    reg = self.fields.lookup_register(field_name, None)
    if reg is None:
        return False
    try:
        reg_val = self.fields.set_field(field_name, field_value)
        toolhead = self.printer.lookup_object("toolhead")
        print_time = toolhead.get_last_move_time()
        self.mcu_tmc.set_register(reg, reg_val, print_time)
        return True
    except Exception:
        logging.exception("tmc_autotune: failed to set %s", field_name)
        return False


def _set_velocity_field(self, field_name, velocity):
    """
    Utility to set a velocity threshold via TMCtstepHelper conversion.
    """
    reg = self.fields.lookup_register(field_name, None)
    if reg is None:
        return False
    try:
        # ensure we have a stepper reference for geometry
        pstepper = self.stepper
        if pstepper is None:
            force_move = self.printer.lookup_object("force_move")
            pstepper = force_move.lookup_stepper(self.stepper_name)
        arg = _tmc.TMCtstepHelper(self.mcu_tmc, velocity, pstepper=pstepper)
        reg_val = self.fields.set_field(field_name, arg)
        toolhead = self.printer.lookup_object("toolhead")
        print_time = toolhead.get_last_move_time()
        self.mcu_tmc.set_register(reg, reg_val, print_time)
        return True
    except Exception:
        logging.exception("tmc_autotune: failed to set velocity field %s", field_name)
        return False


def _compute_pwm_freq_code(fclk, target_hz):
    """
    Select pwm_freq code to keep chopping below the target, mimicking mszturc logic.
    Mapping approximations from patch:
      code: (scale)
        3: 2/410
        2: 2/512
        1: 2/683
        0: 2/1024
    """
    try:
        candidates = [
            (3, 2.0 / 410.0),
            (2, 2.0 / 512.0),
            (1, 2.0 / 683.0),
            (0, 2.0 / 1024.0),
            (0, 0.0),  # fallback
        ]
        for code, scale in candidates:
            if fclk * scale < target_hz:
                return code
    except Exception:
        pass
    return 0


@monkey.patches(_tmc.TMCCommandHelper)
class _KapTmcAutotunePatches(object):
    @monkey.name("__init__")
    def __init__(self, config, mcu_tmc, current_helper):
        # Call original initializer
        call_original(_tmc.TMCCommandHelper, "__init__", self, config, mcu_tmc, current_helper)

        # Cache config and activation gates
        self._kap_cfg = config
        self._kap_printer = config.get_printer()
        section_name = config.get_name()  # e.g., "tmc5160 stepper_x"
        self._kap_driver_type = section_name.split()[0].strip().lower()
        self._kap_stepper_name = self.name  # already set by original

        # Required activation keys
        motor_name = _get_config_optional(config, "get", "motor", None)
        voltage = _get_config_optional(config, "getfloat", "voltage", None, minval=0.0, maxval=60.0)

        # Optional sense resistor source
        cfg_sense = _get_config_optional(config, "getfloat", "sense_resistor", None, minval=0.0)
        stepstick_type = _get_config_optional(config, "get", "stepstick_type", None)

        # User optional knobs (incorporated into calculations if present)
        self._kap_extra_hyst = int(_get_config_optional(config, "getint", "extra_hysteresis", 0, minval=0, maxval=8) or 0)
        self._kap_user_cs = _get_config_optional(config, "getint", "current_scale", None, minval=0, maxval=31)
        # Target PWM frequency
        dflt_target = PWM_FREQ_TARGETS.get(self._kap_driver_type, 55000.0)
        self._kap_pwm_freq_target = float(_get_config_optional(config, "getfloat", "pwm_freq_target", dflt_target, minval=10000.0, maxval=100000.0))

        # Honor user overrides if present (we won't write those fields)
        self._kap_user_override = {
            "TOFF": _has_user_driver_override(config, "TOFF"),
            "TBL": _has_user_driver_override(config, "TBL"),
            "TPFD": _has_user_driver_override(config, "TPFD"),
            "HSTRT": _has_user_driver_override(config, "HSTRT"),
            "HEND": _has_user_driver_override(config, "HEND"),
            "PWM_FREQ": _has_user_driver_override(config, "PWM_FREQ"),
            "PWM_GRAD": _has_user_driver_override(config, "PWM_GRAD"),
            "PWM_OFS": _has_user_driver_override(config, "PWM_OFS"),
            "TPWMTHRS": _has_user_driver_override(config, "TPWMTHRS"),
            "THIGH": _has_user_driver_override(config, "THIGH"),
            # GCONF toggles
            "FASTSTANDSTILL": _has_user_driver_override(config, "FASTSTANDSTILL"),
            "MULTISTEP_FILT": _has_user_driver_override(config, "MULTISTEP_FILT"),
            "SMALL_HYSTERESIS": _has_user_driver_override(config, "SMALL_HYSTERESIS"),
        }

        # If user configured higher-level velocity helpers, skip our TPWMTHRS/THIGH
        if _get_config_optional(config, "getfloat", "stealthchop_threshold", None, minval=0.0) is not None:
            self._kap_user_override["TPWMTHRS"] = True
        if _get_config_optional(config, "getfloat", "high_velocity_threshold", None, minval=0.0) is not None:
            self._kap_user_override["THIGH"] = True

        # Resolve sense resistor/max current
        rsense, max_cur_from_st = _resolve_rsense_and_max_current(current_helper, cfg_sense, stepstick_type)

        # Load motor constants if motor supplied
        motor_obj = None
        if motor_name:
            db_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "motor_database.cfg")
            db = _load_motor_database(db_path)
            spec = db.get(motor_name)
            if spec:
                motor_obj = _MotorConstants(motor_name, spec)
            else:
                logging.warning("tmc_autotune: motor '%s' not found in database %s", motor_name, db_path)

        # Activation gate
        self._kap_enabled = bool(motor_obj and (voltage is not None) and (rsense is not None))

        # Persist references for later runs
        self._kap_motor = motor_obj
        self._kap_voltage = voltage
        self._kap_rsense = rsense
        self._kap_stepstick_type = stepstick_type
        self._kap_max_current_hint = max_cur_from_st
        self._kap_fclk_fallback = 12.5e6

        # Status init
        _STATUS[self._kap_stepper_name] = {
            "enabled": self._kap_enabled,
            "driver": self._kap_driver_type,
            "motor": motor_name,
            "voltage": voltage,
            "rsense": rsense,
            "params": {},
        }

        if self._kap_enabled:
            logging.info("tmc %s ::: Autotune enabled (motor=%s, V=%.3f, Rsense=%.4f)", self._kap_stepper_name, motor_name, voltage, rsense)
        else:
            logging.info("tmc %s ::: Autotune not active (missing motor/voltage/rsense)", self._kap_stepper_name)

    @monkey.name("_handle_connect")
    def _handle_connect(self):
        # Call the original connect handler first
        call_original(_tmc.TMCCommandHelper, "_handle_connect", self)
        # Then perform autotuning if enabled
        try:
            if getattr(self, "_kap_enabled", False):
                self.kap_autotune()
        except Exception:
            logging.exception("tmc %s ::: autotune on connect failed", getattr(self, "_kap_stepper_name", "?"))

    @monkey.name("cmd_SET_TMC_CURRENT")
    def cmd_SET_TMC_CURRENT(self, gcmd):
        # Execute original current update command first
        call_original(_tmc.TMCCommandHelper, "cmd_SET_TMC_CURRENT", self, gcmd)
        # Re-run autotune to reflect new current
        try:
            if getattr(self, "_kap_enabled", False):
                self.kap_autotune()
        except Exception:
            logging.exception("tmc %s ::: autotune after current change failed", getattr(self, "_kap_stepper_name", "?"))

    # New method: perform autotuning and write fields (guarded and override-aware)
    def kap_autotune(self):
        if not getattr(self, "_kap_enabled", False):
            return

        # Obtain run current for calculations
        run_current = None
        try:
            cur_tuple = self.current_helper.get_current()
            # Some helpers return (run, hold, req_hold, max) and tmc2240 returns (run, hold, req_hold, ifs_rms)
            run_current = float(cur_tuple[0])
        except Exception:
            # Fall back to not using current-dependent parts
            run_current = None

        # Get TMC clock frequency or fallback
        try:
            fclk = float(self.mcu_tmc.get_tmc_frequency() or self._kap_fclk_fallback)
        except Exception:
            fclk = self._kap_fclk_fallback

        # Motor constants helper
        motor = self._kap_motor
        if not motor:
            return

        # Resolve driver fields already set or overridden by user
        cfg = self._kap_cfg
        user_tooff = _get_config_optional(cfg, "getint", "driver_TOFF", None, minval=1, maxval=15) if self._kap_user_override["TOFF"] else None
        user_tbl = _get_config_optional(cfg, "getint", "driver_TBL", None, minval=0, maxval=3) if self._kap_user_override["TBL"] else None
        user_tpfd = _get_config_optional(cfg, "getint", "driver_TPFD", None, minval=0, maxval=15) if self._kap_user_override["TPFD"] else None
        user_hstrt = _get_config_optional(cfg, "getint", "driver_HSTRT", None, minval=0, maxval=7) if self._kap_user_override["HSTRT"] else None
        user_hend = _get_config_optional(cfg, "getint", "driver_HEND", None, minval=0, maxval=15) if self._kap_user_override["HEND"] else None

        # Compute pwm_freq code
        try:
            if not self._kap_user_override["PWM_FREQ"]:
                code = _compute_pwm_freq_code(fclk, self._kap_pwm_freq_target)
                _lookup_field_and_set(self, "pwm_freq", code)
        except Exception:
            pass

        # Compute spreadCycle timing
        try:
            ncycles = int(math.ceil(fclk / max(self._kap_pwm_freq_target, 1.0)))
            sdcycles = ncycles / 4.0
            # TOFF: if absent or user didn't override, compute; ensure [1..15]
            toff = user_tooff
            if toff is None:
                toff = max(min(int(math.ceil(max(sdcycles - 24.0, 0.0) / 32.0)), 15), 1)
            # TBL: if toff == 1 and tbl == 0, force tbl = 1 (from patch)
            tbl = user_tbl if user_tbl is not None else 1
            if toff == 1 and (tbl or 0) == 0:
                tbl = 1
            # TPFD
            pfdcycles = ncycles - (24 + 32 * toff) * 2 - [16, 34, 36, 54][min(max(tbl, 0), 3)]
            tpfd = user_tpfd if user_tpfd is not None else max(0, min(15, int(math.ceil(pfdcycles / 128.0))))

            if not self._kap_user_override["TPFD"]:
                _lookup_field_and_set(self, "tpfd", tpfd)
            if not self._kap_user_override["TBL"]:
                _lookup_field_and_set(self, "tbl", tbl)
            if not self._kap_user_override["TOFF"]:
                _lookup_field_and_set(self, "toff", toff)

            # Compute hysteresis (HSTRT/HEND) unless user set them
            if not (self._kap_user_override["HSTRT"] and self._kap_user_override["HEND"]):
                # Use user-provided CS if present, else derive from Rsense + current
                cs = self._kap_user_cs if self._kap_user_cs is not None else 0
                hstrt, hend = motor.hysteresis(
                    name=self._kap_stepper_name,
                    extra=self._kap_extra_hyst,
                    fclk=fclk,
                    volts=float(self._kap_voltage),
                    current=float(run_current or 0.0),
                    tbl=int(tbl),
                    toff=int(toff),
                    rsense=float(self._kap_rsense),
                    scale=int(cs),
                )
                if not self._kap_user_override["HSTRT"]:
                    _lookup_field_and_set(self, "hstrt", hstrt)
                if not self._kap_user_override["HEND"]:
                    _lookup_field_and_set(self, "hend", hend)

        except Exception:
            logging.exception("tmc %s ::: spreadCycle/hysteresis computation failed", self._kap_stepper_name)

        # Compute maxpwmrps and thresholds
        vmaxpwm = None
        try:
            # Get rotation distance (mm/rev)
            force_move = self.printer.lookup_object("force_move")
            pstepper = force_move.lookup_stepper(self.stepper_name)
            rdist, _ = pstepper.get_rotation_distance()
            maxpwmrps = motor.maxpwmrps(fclk=fclk, steps=motor.S, volts=float(self._kap_voltage), current=float(run_current or 0.0))
            vmaxpwm = float(maxpwmrps) * float(rdist)

            # THIGH = 1.2 * vmaxpwm unless user provided high_velocity_threshold
            if not self._kap_user_override["THIGH"]:
                vhigh = 1.2 * vmaxpwm
                _set_velocity_field(self, "thigh", vhigh)

            # TPWMTHRS = 0xfffff unless user provided stealthchop_threshold
            if not self._kap_user_override["TPWMTHRS"]:
                _lookup_field_and_set(self, "tpwmthrs", 0xFFFFF)

        except Exception:
            logging.exception("tmc %s ::: vmaxpwm/threshold computation failed", self._kap_stepper_name)

        # PWM parameters
        try:
            # PWM_GRAD derived from motor constants and fclk/steps/voltage
            if not self._kap_user_override["PWM_GRAD"]:
                grad = motor.pwmgrad(fclk=fclk, steps=motor.S, volts=float(self._kap_voltage))
                _lookup_field_and_set(self, "pwm_grad", int(max(0, min(255, grad))))
            # PWM_OFS derived from voltage and current
            if not self._kap_user_override["PWM_OFS"]:
                ofs = motor.pwmofs(volts=float(self._kap_voltage), current=float(run_current or 0.0))
                _lookup_field_and_set(self, "pwm_ofs", int(max(0, min(255, ofs))))

            # Always enable autoscale/autograd (if present), unless user set
            _lookup_field_and_set(self, "pwm_autoscale", True)
            _lookup_field_and_set(self, "pwm_autograd", True)

            # Conservative reg/lim defaults from mszturc patch (if present)
            _lookup_field_and_set(self, "pwm_reg", 15)
            _lookup_field_and_set(self, "pwm_lim", 4)
        except Exception:
            logging.exception("tmc %s ::: PWM configuration failed", self._kap_stepper_name)

        # Mode flags (safe toggles; skip if user set)
        try:
            if not self._kap_user_override["FASTSTANDSTILL"]:
                _lookup_field_and_set(self, "faststandstill", True)
            if not self._kap_user_override["SMALL_HYSTERESIS"]:
                _lookup_field_and_set(self, "small_hysteresis", False)
            if not self._kap_user_override["MULTISTEP_FILT"]:
                _lookup_field_and_set(self, "multistep_filt", True)
        except Exception:
            pass

        # Update status
        _STATUS[self._kap_stepper_name]["params"] = {
            "pwm_freq_target": self._kap_pwm_freq_target,
            "vmaxpwm": vmaxpwm,
            "extra_hysteresis": self._kap_extra_hyst,
        }

        logging.info("tmc %s ::: autotune complete%s",
                     self._kap_stepper_name,
                     "" if vmaxpwm is None else f" (vmaxpwm={vmaxpwm:.4f} mm/s)")

    # Expose a small status view via TMCCommandHelper.get_status merge
    @monkey.name("get_status")
    def get_status(self, eventtime=None):
        base = call_original(_tmc.TMCCommandHelper, "get_status", self, eventtime)
        # Augment status with autotune data
        try:
            auto = _STATUS.get(self._kap_stepper_name, {})
            base = dict(base or {})
            base["kap_tmc_autotune"] = {
                "enabled": bool(auto.get("enabled")),
                "driver": auto.get("driver"),
                "motor": auto.get("motor"),
                "voltage": auto.get("voltage"),
                "rsense": auto.get("rsense"),
                "params": auto.get("params", {}),
            }
        except Exception:
            pass
        return base


def load_config(config):
    # Bootstrap plugin: apply patches and expose a minimal status object
    def _status():
        # summarized view across all tuned steppers
        return {"steppers": list(_STATUS.keys())}

    return bootstrap_plugin(
        __import__(__name__),
        config,
        status={"kap_tmc_autotune": _status()},
    )