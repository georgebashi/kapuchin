# Kapuchin plugin: bed_mesh_check
#
# Adds a BED_MESH_CHECK gcode command without modifying core code by
# monkey-patching BedMesh via Monkey.
#
# Behavior:
#   - Validates mesh deviation (MAX_DEVIATION)
#   - Validates maximum slope between adjacent points (MAX_SLOPE)
#   - Registers command on the existing BedMesh instance at plugin load
#     and also on any future BedMesh instances via a patched __init__.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
from ..extras import kapuchin_monkey as monkey
from ..extras import bed_mesh
from ..extras.kapuchin import bootstrap_plugin, call_original


def _register_command_for_instance(bm):
    """
    Register BED_MESH_CHECK command on a BedMesh instance (idempotent).
    """
    if getattr(bm, "_kapuchin_bmc_registered", False):
        return
    help_str = "Validate a variety of bed mesh parameters"
    # Provide a help string attribute consistent with Klipper conventions
    setattr(bm, "cmd_BED_MESH_CHECK_help", help_str)
    bm.gcode.register_command("BED_MESH_CHECK", bm.cmd_BED_MESH_CHECK, desc=help_str)
    bm._kapuchin_bmc_registered = True


@monkey.patches(bed_mesh.BedMesh)
class _BedMeshCheckPatches(object):
    # Defensive registration for any BedMesh created after plugin load.
    @monkey.name("__init__")
    def __init__(self, config):
        call_original(bed_mesh.BedMesh, "__init__", self, config)
        try:
            _register_command_for_instance(self)
        except Exception:
            logging.exception("bed_mesh_check: registration in BedMesh.__init__ failed")

    # Add the BED_MESH_CHECK gcode handler to the BedMesh class.
    @monkey.name("cmd_BED_MESH_CHECK")
    def cmd_BED_MESH_CHECK(self, gcmd):
        """
        BED_MESH_CHECK [MAX_DEVIATION=<float>] [MAX_SLOPE=<float>]

        - MAX_DEVIATION: Ensure (max_z - min_z) does not exceed the given value.
        - MAX_SLOPE: Ensure the max slope between adjacent grid points does not
                     exceed the given value (mm/mm).
        """
        # Require an active mesh
        if self.z_mesh is None:
            raise self.gcode.error("No mesh has been loaded")

        has_checks = False

        # Deviation check
        max_deviation = gcmd.get_float("MAX_DEVIATION", None)
        if max_deviation is not None:
            has_checks = True
            if max_deviation <= 0:
                raise self.gcode.error("MAX_DEVIATION must be greater than 0")

            mesh_min, mesh_max = self.z_mesh.get_z_range()
            current_deviation = mesh_max - mesh_min

            if current_deviation > max_deviation:
                message = (
                    f"Mesh deviation ({current_deviation:.6f}) exceeds maximum "
                    f"allowed deviation ({max_deviation:.6f})"
                )
                raise self.gcode.error(message)
            else:
                gcmd.respond_info(
                    f"Mesh deviation ({current_deviation:.6f}) is within the "
                    f"allowed maximum ({max_deviation:.6f})"
                )

        # Slope check (MAX_SLOPE)
        max_slope = gcmd.get_float("MAX_SLOPE", None)
        if max_slope is not None:
            has_checks = True
            if max_slope <= 0:
                raise self.gcode.error("MAX_SLOPE must be greater than 0")

            # Gather mesh data
            mesh_matrix = self.z_mesh.get_mesh_matrix() or [[]]
            params = self.z_mesh.get_mesh_params()

            rows = len(mesh_matrix)
            cols = len(mesh_matrix) if rows > 0 else 0

            # Compute spacing based on the rendered mesh size so adjacent cells
            # are measured with consistent physical distances.
            x_dist = None
            y_dist = None
            if cols > 1:
                x_dist = (params["max_x"] - params["min_x"]) / float(cols - 1)
            if rows > 1:
                y_dist = (params["max_y"] - params["min_y"]) / float(rows - 1)

            max_slope_value = 0.0
            max_slope_pos = None

            # Check slopes in X direction (left/right neighbors)
            if x_dist and x_dist > 0:
                for y in range(rows):
                    row = mesh_matrix[y]
                    for x in range(cols - 1):
                        z1 = row[x]
                        z2 = row[x + 1]
                        slope = abs((z2 - z1) / x_dist)
                        if slope > max_slope_value:
                            max_slope_value = slope
                            max_slope_pos = (x, y, x + 1, y)

            # Check slopes in Y direction (up/down neighbors)
            if y_dist and y_dist > 0 and cols > 0:
                for x in range(cols):
                    for y in range(rows - 1):
                        z1 = mesh_matrix[y][x]
                        z2 = mesh_matrix[y + 1][x]
                        slope = abs((z2 - z1) / y_dist)
                        if slope > max_slope_value:
                            max_slope_value = slope
                            max_slope_pos = (x, y, x, y + 1)

            if max_slope_value > max_slope:
                # Report offending segment, translating indices to bed coords
                if max_slope_pos is not None and x_dist and y_dist:
                    x1 = params["min_x"] + max_slope_pos * x_dist
                    y1 = params["min_y"] + max_slope_pos * y_dist
                    x2 = params["min_x"] + max_slope_pos * x_dist
                    y2 = params["min_y"] + max_slope_pos * y_dist
                    message = (
                        f"Maximum slope ({max_slope_value:.6f} mm/mm) between points "
                        f"({x1:.2f},{y1:.2f}) and ({x2:.2f},{y2:.2f}) "
                        f"exceeds allowed maximum ({max_slope:.6f} mm/mm)"
                    )
                else:
                    message = (
                        f"Maximum slope ({max_slope_value:.6f} mm/mm) exceeds allowed "
                        f"maximum ({max_slope:.6f} mm/mm)"
                    )
                raise self.gcode.error(message)
            else:
                gcmd.respond_info(
                    f"Maximum slope ({max_slope_value:.6f} mm/mm) is within the "
                    f"allowed maximum ({max_slope:.6f} mm/mm)"
                )

        if not has_checks:
            gcmd.respond_info(
                "No validation checks specified. Available checks:\n"
                "MAX_DEVIATION - Validate maximum mesh height deviation\n"
                "MAX_SLOPE - Validate maximum slope between adjacent points"
            )


def _after_patch_hook(config):
    # Register command on already-constructed BedMesh instance (if any)
    printer = config.get_printer()
    bedmesh = printer.lookup_object("bed_mesh", None)
    if bedmesh is not None:
        try:
            _register_command_for_instance(bedmesh)
            logging.info("bed_mesh_check: command registered on existing BedMesh")
        except Exception:
            logging.exception("bed_mesh_check: failed registering on existing BedMesh instance")


def load_config(config):
    return bootstrap_plugin(
        __import__(__name__),
        config,
        after_patch=_after_patch_hook,
        status={"command": "BED_MESH_CHECK"}
    )