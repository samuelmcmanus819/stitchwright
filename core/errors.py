"""Shared exception base for StitchWright's conversion modules."""


class StitchwrightError(Exception):
    """A problem worth showing the user verbatim (bad input, unsupported
    option, nothing to do) — as opposed to a bug, which should raise
    something else and surface as a real traceback."""
