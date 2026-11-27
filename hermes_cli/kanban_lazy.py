"""Late module/handler imports for the board CLI's read path."""
from importlib import import_module


class Module:
    def __init__(self, name):
        self.name = name

    def __getattr__(self, name):
        return getattr(import_module(self.name), name)


def handler(module, name):
    def call(*args, **kwargs):
        return getattr(import_module(module), name)(*args, **kwargs)
    return call
