"""Point antigua_core's runtime data at a throwaway directory.

Every suite imports this first, before antigua_core, so memories, lists,
timers and caches written during a test land in a temp dir instead of the
live data/ files. Honors an ANTIGUA_DATA_DIR that's already set.
"""

import os
import tempfile

os.environ.setdefault("ANTIGUA_DATA_DIR", tempfile.mkdtemp(prefix="antigua_test_data_"))
