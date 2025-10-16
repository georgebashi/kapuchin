# Kapuchin Plugin Framework: Developer Guide

## 1. Introduction

Welcome to the Kapuchin Plugin Framework for Klipper. This framework provides a powerful way to extend and modify Klipper's functionality without maintaining a full fork of the Klipper source code. By using a monkey-patching approach, Kapuchin allows you to inject your own code into Klipper at runtime, keeping your custom features separate from the core Klipper codebase.

This guide is intended for developers who are already familiar with Klipper's architecture and want to create their own plugins using the Kapuchin framework.

## 2. Getting Started: "Hello, World!" Plugin

Let's create a simple plugin that adds a new G-code command, `SAY_HELLO`, to Klipper.

### Step 1: Create the Plugin File

Create a new Python file in the `klippy/patches/` directory named `hello_world.py`.

**`klippy/patches/hello_world.py`:**
```python
# patches/hello_world.py
from klippy.extras.kapuchin_monkey import monkey
import logging

# Find the GCode module in Klipper's printer object
def find_gcode_module(printer):
    for name, obj in printer.objects.items():
        if 'gcode' in name:
            return obj
    return None

class HelloWorld:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = find_gcode_module(self.printer)
        if self.gcode:
            self.gcode.register_command("SAY_HELLO", self.cmd_SAY_HELLO, desc=self.cmd_SAY_HELLO_help)
        else:
            logging.warning("Could not find gcode module to register SAY_HELLO")

    cmd_SAY_HELLO_help = "A friendly greeting from your first Kapuchin plugin"
    def cmd_SAY_HELLO(self, gcmd):
        self.gcode.respond_info("Hello, World!")

def load_config(config):
    return HelloWorld(config)
```

### Step 2: Configure the Plugin in `printer.cfg`

Add the following section to your `printer.cfg` file to enable the plugin:

```ini
[patch hello_world]
```

### Step 3: Restart Klipper

Restart your Klipper instance for the changes to take effect. You can now run `SAY_HELLO` in your printer's console, and you should see the message "Hello, World!".

### Explanation

*   **`klippy/patches/hello_world.py`**: This file contains the logic for our plugin. The `load_config` function is the entry point for the plugin, which Kapuchin calls when it loads the `[patch hello_world]` section.
*   **`[patch hello_world]`**: This section in `printer.cfg` tells Kapuchin to load the `hello_world.py` module from the `patches` directory.
*   **`HelloWorld` class**: This class encapsulates our plugin's logic. In its `__init__` method, it registers the `SAY_HELLO` G-code command.

## 3. Core Concepts

### Plugin Loading

The Kapuchin framework integrates into Klipper's startup process to load plugins. Here's how it works:

1.  **`[kapuchin]` section**: The presence of a `[kapuchin]` section in `printer.cfg` activates the framework.
2.  **Monkey-Patching Klipper**: Kapuchin monkey-patches Klipper's configuration handling to inject its own plugin loading logic.
3.  **Plugin Discovery**: Kapuchin scans your `printer.cfg` for any sections that start with `patch `, for example, `[patch my_plugin]`.
4.  **Module Import**: For each `[patch ...]` section found, Kapuchin imports the corresponding Python module from the `klippy/patches/` directory (e.g., `patches.my_plugin`).
5.  **Initialization**: Kapuchin then calls a `load_config(config)` or `load_config_prefix(config)` function within your plugin's module.

### Monkey-Patching

Monkey-patching is the process of modifying or extending code at runtime. Kapuchin uses this technique to alter Klipper's behavior without changing the original source code. The `kapuchin_monkey.py` module provides decorators to make this easy.

*   **`@monkey.patch(destination, name=None)`**: Use this decorator on a function or class to replace an attribute on the `destination` class.
*   **`@monkey.patches(destination)`**: Use this decorator on a class to indicate that all methods within your class are patches for the `destination` class.

### Extending vs. Replacing

When patching a method, you can either replace it entirely or extend its functionality.

*   **Replacing a Method**: If you define a patch with the same name as an existing method, you will completely overwrite the original.
*   **Extending a Method**: To call the original method from within your patch, use the `kapuchin.call_original()` helper function. This allows you to add behavior before or after the original code runs.

**Example of Extending `__init__`:**

```python
from klippy.extras.kapuchin import call_original
from klippy.extras.kapuchin_monkey import monkey
from klippy.kinematics import cartesian

@monkey.patches(cartesian.CartesianKinematics)
class MyKinematicsPatch:
    def __init__(self, toolhead, config):
        # Call the original __init__ method first
        call_original(cartesian.CartesianKinematics, "__init__", self, toolhead, config)
        
        # Add your custom initialization logic here
        self.printer = config.get_printer()
        self.printer.lookup_object('gcode').respond_info("My custom kinematics patch is loaded!")
```

## 4. API Reference

### `kapuchin.py` Helpers

*   **`bootstrap_plugin(module, config, original_printer_object_name)`**:
    A helper for simple plugins. It automates the process of finding the target Klipper object and applying the patches defined in the `module`.

*   **`call_original(cls, name, self, *args, **kwargs)`**:
    Calls the original, un-patched version of a method.
    *   `cls`: The class that was patched.
    *   `name`: The name of the method to call (e.g., `"__init__"`).
    *   `self`: The instance of the class.
    *   `*args`, `**kwargs`: The original arguments to the method.

### `kapuchin_monkey.py` Decorators

*   **`@monkey.patch(destination, name=None)`**:
    A decorator to mark a function or class as a patch.
    *   `destination`: The class to be patched.
    *   `name`: The name of the attribute to patch. If `None`, the name of the decorated function is used.

*   **`@monkey.patches(destination)`**:
    A decorator for a class that marks all of its methods as patches for the `destination` class.

## 5. Common Patterns & Best Practices

*   **Patching `__init__`**: The most common pattern is to patch the `__init__` method of a Klipper class. This allows you to run setup code, register G-code commands, and get references to other Klipper objects.
*   **Use `call_original`**: When patching `__init__`, it's almost always a good idea to use `call_original` to ensure that the original class is set up correctly.
*   **Structure Your Plugin**: For clarity, encapsulate your plugin's logic within a class. Use the `@monkey.patches` decorator on your class to keep your patches organized.
*   **Use `bootstrap_plugin` for Simplicity**: If your plugin only needs to patch a single Klipper object, `bootstrap_plugin` can simplify your `load_config` function.