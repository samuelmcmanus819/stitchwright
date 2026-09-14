"""Shared utilities used by both conversion modules (`png_to_embroidery.py`
and `pattern_tiler.py`): error base class, image I/O, and print-canvas unit
math. Kept dependency-light and filesystem-free so it loads unchanged under
Pyodide in the browser, same as the modules that import it.
"""
