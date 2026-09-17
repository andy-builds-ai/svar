"""Shared test setup.

Lambda imports the answer function's modules by their plain names, ``handler``
and ``aws``, because it packages the app/ folder as its import root. The tests
put the same folder on the import path, so they import exactly what Lambda
imports, under the same names.
"""

import sys
from pathlib import Path

APP_FOLDER = Path(__file__).resolve().parents[1] / "app"

if str(APP_FOLDER) not in sys.path:
    sys.path.insert(0, str(APP_FOLDER))
