# Example patch for Kapuchin plugin system
#
# Copyright (C) 2024 George Jetson <george.jetson@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging

class ExamplePatch:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.message = config.get('message', 'Hello from Kapuchin!')
        logging.info(f"ExamplePatch loaded with message: {self.message}")

    def get_status(self, eventtime=None):
        return {"message": self.message}

def load_config(config):
    return ExamplePatch(config)

def load_config_prefix(config):
    return ExamplePatch(config)