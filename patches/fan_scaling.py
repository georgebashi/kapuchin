# Kapuchin plugin: fan_scaling
#
# Implements PWM scaling for Fan so that requests map to [off_below, max_power]
# without modifying core code. Uses Monkey to monkey-patch Fan._apply_speed
# and enforces off_below <= max_power via Fan.__init__ validation.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

from extras import kapuchin_monkey as monkey
from extras import fan
from extras.kapuchin import call_original


@monkey.patches(fan.Fan)
class _FanScalingPatches(object):
    # Replace Fan._apply_speed using recommended decorator style and retrieve
    # the original attribute via monkey.get_original_attribute.
    @monkey.name('_apply_speed')
    def _apply_speed(self, print_time, value):
        """
        Map requested value r in to proxy p such that core computes:
            final = p * max_power = off_below + r * (max_power - off_below)
        Then delegate to the original _apply_speed with p.
        """
        r = value
        if r > 0.0:
            off_below = getattr(self, 'off_below', 0.0) or 0.0
            max_power = getattr(self, 'max_power', 1.0) or 1.0
            # Avoid division by zero and clamp base within
            if max_power <= 0.0:
                p = 0.0
            else:
                base = off_below / max_power
                if base < 0.0:
                    base = 0.0
                elif base > 1.0:
                    base = 1.0
                p = base + r * (1.0 - base)
                if p < 0.0:
                    p = 0.0
                elif p > 1.0:
                    p = 1.0
            return call_original(fan.Fan, '_apply_speed', self, print_time, p)
        else:
            return call_original(fan.Fan, '_apply_speed', self, print_time, 0.0)

    # Add strict validation to Fan.__init__ so any future instantiations are checked.
    @monkey.name('__init__')
    def __init__(self, _cfg, default_shutdown_speed=0.):
        # Initialize first, then validate derived attributes
        call_original(fan.Fan, '__init__', self, _cfg, default_shutdown_speed)
        try:
            ob = float(self.off_below)
            mp = float(self.max_power)
        except Exception:
            ob = getattr(self, 'off_below', 0.0)
            mp = getattr(self, 'max_power', 1.0)
        if ob > mp:
            raise _cfg.error(
                "off_below=%f can't be larger than max_power=%f" % (ob, mp)
            )