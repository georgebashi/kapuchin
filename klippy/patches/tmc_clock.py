# Kapuchin plugin: tmc_clock
#
# Extends the get_status command for TMC drivers to include the
# clock frequency.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
from extras import kapuchin_monkey as monkey
from extras import tmc as _tmc
from extras.kapuchin import bootstrap_plugin, call_original

@monkey.patches(_tmc.TMCCommandHelper)
class _KapTmcClockPatches(object):
    @monkey.name("get_status")
    def get_status(self, eventtime=None):
        # Call the original get_status method
        base = call_original(_tmc.TMCCommandHelper, "get_status", self, eventtime)

        # Add the tmc_frequency to the status dictionary
        try:
            frequency = self.mcu_tmc.get_tmc_frequency()
            base = dict(base or {})
            base["tmc_frequency"] = frequency
        except Exception:
            logging.exception("tmc_clock: could not get tmc_frequency")

        return base

def load_config(config):
    # Bootstrap the plugin to apply the patches
    return bootstrap_plugin(
        __import__(__name__),
        config,
    )