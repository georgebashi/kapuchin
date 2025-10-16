# Kapuchin plugin: tmc5160_short_conf
#
# Re-implements the upstream patch for TMC5160 SHORT_CONF using Kapuchin
# without modifying core files. It:
#  - Adds FieldHelper optional-field behavior (default=None => skip write).
#  - Adds SHORT_CONF bitfield mapping for TMC5160 (if missing).
#  - Injects constructor-time logic to program SHORT_CONF when both level
#    keys are present; otherwise raises when any SHORT_CONF key is present
#    without both level keys.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

from ..extras import kapuchin_monkey as monkey
from ..extras import tmc as _tmc
from ..extras import tmc5160 as _tmc5160
from ..extras.kapuchin import bootstrap_plugin, call_original


@monkey.patches(_tmc.FieldHelper)
class _FieldHelperPatches(object):
    # Replace FieldHelper.set_config_field to add optional default=None semantics
    @monkey.name("set_config_field")
    def set_config_field(self, config, field_name, default):
        """
        Allow a field to be set from the config file.

        Upstream patch behavior: If default is None and the config key is not
        provided (val is None), return early (do not write).
        """
        # Compute config key and bounds as in core implementation
        config_name = "driver_" + field_name.upper()
        reg_name = self.field_to_register[field_name]
        mask = self.all_fields[reg_name][field_name]
        # max value for this field
        maxval = mask >> _tmc.ffs(mask)
        # original typed parsing paths
        if maxval == 1:
            val = config.getboolean(config_name, default)
        elif field_name in self.signed_fields:
            val = config.getint(config_name, default,
                                minval=-(maxval // 2 + 1), maxval=maxval // 2)
        else:
            val = config.getint(config_name, default, minval=0, maxval=maxval)
        # Added by tmc_short_conf.patch:
        if default is None and val is None:
            return
        # Write computed value into field/register set
        return self.set_field(field_name, val)


@monkey.patches(_tmc5160.TMC5160)
class _TMC5160Patches(object):
    @monkey.name("__init__")
    def __init__(self, config):
        """
        Wrapper that runs core initialization, then applies SHORT_CONF logic
        equivalent to the upstream patch.
        """
        call_original(_tmc5160.TMC5160, "__init__", self, config)

        # Mirror upstream variable names/usage
        set_config_field = self.fields.set_config_field

        # Gate: require both levels to be present (and in-range) to program SHORT_CONF
        # Using the same min/max arguments as the upstream diff:
        s2vs_ok = config.getint("driver_s2vs_level", None, 4, 15)
        s2g_ok = config.getint("driver_s2g_level", None, 2, 15)

        if s2vs_ok and s2g_ok:
            # Write-only SHORT_CONF: program fixed values per patch
            # Upstream used set_config_field for each bitfield; do the same API
            # but we could also directly compute and set the register.
            set_config_field(config, "s2vs_level", 6)
            set_config_field(config, "s2g_level", 6)
            set_config_field(config, "short_filter", 1)
            set_config_field(config, "shortdelay", 0)
            return

        # If any SHORT_CONF-related key present without both levels, raise error
        short_keys = ("s2vs_level", "s2g_level", "short_filter", "shortdelay")
        if any(config.get("driver_%s" % k, None, False) for k in short_keys):
            raise config.error(
                "driver_s2vs_level and driver_s2g_level are required to update short_conf"
            )


def _before_patch_hook(config):
    # Ensure SHORT_CONF field mapping exists (upstream adds this)
    _tmc5160.Fields.setdefault("SHORT_CONF", {
        "s2vs_level":   0x0F << 0,
        "s2g_level":    0x0F << 8,
        "short_filter": 0x03 << 16,
        "shortdelay":   0x01 << 18,
    })


def load_config(config):
    return bootstrap_plugin(
        __import__(__name__),
        config,
        before_patch=_before_patch_hook,
        status={'short_conf': True}
    )