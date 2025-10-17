# Kapuchin extras loader and core (merged)
#
# Loads Monkey-based patching when a [kapuchin] section is present and
# provides the KapuchinManager that loads [patch ...] plugins just before
# configuration validation.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import importlib
import logging
import os

# Name of the Kapuchin manager in printer.objects
KAPUCHIN_MANAGER_NAME = "kapuchin_manager"


class KapuchinManager:
    def __init__(self, printer):
        self.printer = printer
        self.patches_loaded = False

    def get_status(self, eventtime=None):
        return {
            "patches_loaded": self.patches_loaded,
        }

    def load_plugins(self, config):
        # Loads all [patch ...] sections. Runs at most once per read cycle.
        if self.patches_loaded:
            return
        self.patches_loaded = True

        patch_sections = config.get_prefix_sections('patch ')
        for section_config in patch_sections:
            section_name = section_config.get_name()
            try:
                self._load_plugin(section_config)
            except config.error:
                logging.exception("Error loading patch from section '%s'", section_name)
                raise
            except Exception as e:
                logging.exception("Unhandled error in patch '%s'", section_name)
                raise config.error("Unhandled error in patch '%s': %s" % (section_name, e))

    def _load_plugin(self, config):
        # Load a single [patch ...] plugin by importing patches.<module>
        section_name = config.get_name()
        module_parts = section_name.split()
        if len(module_parts) < 2:
            raise config.error("Invalid patch section name: '%s'" % (section_name,))

        module_name = module_parts[1]
        base_dir = os.path.dirname(os.path.dirname(__file__))
        py_name = os.path.join(base_dir, 'patches', module_name + '.py')

        if not os.path.exists(py_name):
            raise config.error("Unable to load patch module '%s'" % (module_name,))

        mod = importlib.import_module('patches.' + module_name)
        init_func_name = 'load_config'
        if len(module_parts) > 2:
            init_func_name = 'load_config_prefix'

        init_func = getattr(mod, init_func_name, None)
        if init_func is None:
            # Fallback: if load_config is missing, bootstrap anyway
            plugin_obj = bootstrap_plugin(mod, config)
        else:
            # Register plugin object under the full section name to satisfy validation
            plugin_obj = init_func(config)

        self.printer.add_object(section_name, plugin_obj or object())
        logging.info("Kapuchin: Loaded patch '%s'", section_name)


def install():
    """
    Install the Monkey patch that intercepts PrinterConfig.check_unused_options()
    to load [patch ...] plugins just before config validation.
    """
    # Import here to avoid side effects during import_test()
    from . import kapuchin_monkey as monkey
    import configfile

    original_check_unused = configfile.PrinterConfig.check_unused_options

    @monkey.patch(configfile.PrinterConfig)
    def check_unused_options(self, config):
        printer = self.get_printer()
        manager = printer.lookup_object(KAPUCHIN_MANAGER_NAME, None)
        if manager is None or not hasattr(manager, 'load_plugins'):
            manager = KapuchinManager(printer)
            printer.add_object(KAPUCHIN_MANAGER_NAME, manager)

        # Load all [patch ...] sections before running validation
        manager.load_plugins(config)

        # Call original validator
        original_check_unused(self, config)

    # Always overwrite and keep original stored internally
    monkey.apply(monkey.Patch(configfile.PrinterConfig, 'check_unused_options', check_unused_options))


class KapuchinExtrasSentinel:
    # Small sentinel object so [kapuchin] is a valid config section and can report status
    def __init__(self, config):
        self.printer = config.get_printer()

    def get_status(self, eventtime):
        # Expose basic status if KapuchinManager has been created by the patch
        mgr = self.printer.lookup_object(KAPUCHIN_MANAGER_NAME, None)
        if mgr is None:
            return {'installed': True, 'patches_loaded': False}
        try:
            status = mgr.get_status(eventtime)
            res = {'installed': True}
            res.update(status)
            return res
        except Exception:
            return {'installed': True}


def load_config(config):
    """
    Extras entrypoint: called when a [kapuchin] section exists in printer.cfg.
    Installs the Monkey patch and returns a sentinel object.
    """
    install()
    logging.info("Kapuchin: extras loader installed Monkey patch")
    return KapuchinExtrasSentinel(config)


def bootstrap_plugin(module, config, before_patch=None, after_patch=None, status=None):
    """
    Bootstrap a minimal patch plugin.

    - Finds and applies all Monkey patches in the given module.
    - Optionally runs before/after hooks.
    - Returns a minimal plugin object with a get_status() method.
    """
    from . import kapuchin_monkey as monkey

    if before_patch:
        before_patch(config)

    patches = monkey.find_patches([module])
    for patch in patches:
        monkey.apply(patch)

    if after_patch:
        after_patch(config)

    class _Plugin:
        def __init__(self, _cfg):
            pass

        def get_status(self, eventtime=None):
            res = {"enabled": True}
            if status:
                res.update(status)
            return res

    return _Plugin(config)


def call_original(cls, name, self, *args, **kwargs):
    """
    Convenience helper to call the original (pre-patch) method from within a Monkey patch.

    Examples:
      - Call a base __init__:
          call_original(bed_mesh.BedMesh, "__init__", self, config)

      - Delegate to a core method:
          call_original(fan.Fan, "_apply_speed", self, print_time, p)
    """
    from . import kapuchin_monkey as monkey
    original = monkey.get_original_attribute(cls, name)
    return original(self, *args, **kwargs)


def load_config_prefix(config):
    # Not expected to be used (e.g. [kapuchin something]) — treat same as load_config
    return load_config(config)