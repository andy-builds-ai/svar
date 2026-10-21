"""Operating scripts for Andur."""

import sys


def utf8_output() -> None:
    """Write stdout and stderr as UTF-8, also when they are piped or redirected; a lone
    surrogate a model wrote is printed as its escape instead of ending the script."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="backslashreplace")
