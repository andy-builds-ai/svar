"""The clocks the function reads and its one wait, in one place so that a test can set them."""

import time
from collections.abc import Callable

#: Seconds that only run forward: how old a cached key is, how long a request has run.
clock: Callable[[], float] = time.monotonic

#: Seconds since the epoch: the key budgets' windows and the plans' days run on it.
wall_clock: Callable[[], float] = time.time

#: The wait before a model call AWS turned away is made once more.
pause: Callable[[float], None] = time.sleep
