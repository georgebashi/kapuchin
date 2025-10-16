# Kapuchin plugin: set_heater_pid
#
# Allows modifying PID parameters of heaters on the fly without a reload.
# This is a monkey-patch implementation of the feature originally
# introduced in commit e886821bfc3fbd686ad3406f1f5825221d16c29e.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import gorilla
from ..extras import heaters
from ..extras.kapuchin import call_original

PID_PARAM_BASE = 255.0


@gorilla.patches(heaters.Heater)
class _SetHeaterPIDPatch:
    @gorilla.name("__init__")
    def __init__(self, config, parent_heater=None):
        call_original(heaters.Heater, "__init__", self, config, parent_heater)

        gcode = self.printer.lookup_object("gcode")
        gcode.register_mux_command(
            "SET_HEATER_PID",
            "HEATER",
            self.name,
            self.cmd_SET_HEATER_PID,
            desc=self.cmd_SET_HEATER_PID_help,
        )

    cmd_SET_HEATER_PID_help = "Sets a heater PID parameter"

    def cmd_SET_HEATER_PID(self, gcmd):
        if not isinstance(self.control, heaters.ControlPID):
            raise gcmd.error("Not a PID controlled heater")
        kp = gcmd.get_float("KP", None)
        if kp is not None:
            self.control.Kp = kp / PID_PARAM_BASE
        ki = gcmd.get_float("KI", None)
        if ki is not None:
            self.control.Ki = ki / PID_PARAM_BASE
        kd = gcmd.get_float("KD", None)
        if kd is not None:
            self.control.Kd = kd / PID_PARAM_BASE
