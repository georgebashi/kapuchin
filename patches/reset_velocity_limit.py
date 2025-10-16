# Kapuchin plugin: reset_velocity_limit
#
# Implements a RESET_VELOCITY_LIMIT command to restore original velocity
# settings. Uses Monkey to monkey-patch ToolHead.__init__ to save the
# original config and adds the new G-code command.
#
# This file may be distributed under the terms of the GNU GPLv3 license.
from ..extras import kapuchin_monkey as monkey
from ..toolhead import ToolHead
from ..extras.kapuchin import call_original


@monkey.patches(ToolHead)
class _ResetVelocityLimitPatches(object):
    # Patch __init__ to store original velocity configuration
    @monkey.name('__init__')
    def __init__(self, config):
        call_original(ToolHead, '__init__', self, config)
        self.orig_cfg = {
            "max_velocity": self.max_velocity,
            "max_accel": self.max_accel,
            "min_cruise_ratio": getattr(self, 'min_cruise_ratio', 0.0),
            "square_corner_velocity": self.square_corner_velocity
        }

    # Add the new cmd_RESET_VELOCITY_LIMIT method to ToolHead
    def cmd_RESET_VELOCITY_LIMIT(self, gcmd):
        self.max_velocity = self.orig_cfg["max_velocity"]
        self.max_accel = self.orig_cfg["max_accel"]
        if "min_cruise_ratio" in self.orig_cfg:
            self.min_cruise_ratio = self.orig_cfg["min_cruise_ratio"]
        self.square_corner_velocity = self.orig_cfg["square_corner_velocity"]
        self._calc_junction_deviation()
        msg = (
            "max_velocity: %.6f" % self.max_velocity,
            "max_accel: %.6f" % self.max_accel,
            "minimum_cruise_ratio: %.6f" % getattr(self, 'min_cruise_ratio', 0.0),
            "square_corner_velocity: %.6f" % self.square_corner_velocity,
        )
        gcmd.respond_info("\n".join(msg), log=False)
    cmd_RESET_VELOCITY_LIMIT_help = "Reset printer velocity limits"

    # Patch register_gcode_handlers to add the new command
    @monkey.name('register_gcode_handlers')
    def register_gcode_handlers(self):
        call_original(ToolHead, 'register_gcode_handlers', self)
        gcode = self.printer.lookup_object('gcode')
        gcode.register_command(
            "RESET_VELOCITY_LIMIT",
            self.cmd_RESET_VELOCITY_LIMIT,
            desc=self.cmd_RESET_VELOCITY_LIMIT_help,
        )
