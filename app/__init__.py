"""The Andur answer function: handler.py, aws.py and plans.py.

Neither Lambda nor the tests import this folder as a package: both import
the modules by their plain names, handler, aws and plans (tests/conftest.py).
This file is there for scripts/workspace.py, which reads the plans as
``app.plans``."""
