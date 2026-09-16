"""Operating scripts for Andur. Run them from the repository root as modules,
for example ``python -m scripts.ingest``."""

import sys


def utf8_output() -> None:
    """Write stdout and stderr as UTF-8, also when they are piped or redirected.

    On Windows, Python writes to a pipe or file in the legacy code page, which
    turns the dashes and umlauts in the corpus into garbage. An interactive
    console is not affected either way.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")
