"""Kilnkit edition marker.

Kilnkit ships as two builds, **full** and **lite**. The lite build simply leaves
``render_queue.py`` out of the package. There is no runtime lock and no licence
check anywhere in the code — the build split is the whole mechanism, so the
GPL-licensed source stays freely modifiable in either edition.

This constant is a label only. The real capability signal is whether the
``render_queue`` module imported at all; ``ui.py`` guards on that, and the test
harness reads this constant to confirm it is looking at the build it expects.
"""

EDITION = "lite"
