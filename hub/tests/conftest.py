"""Put the hub package root on sys.path.

hub.py is a script, not an installed package, and there is no pyproject in
hub/ to carry a `pythonpath` setting. pytest inserts the TEST directory on
sys.path, not its parent, so without this `import hub` picks up whatever else
is called hub on the interpreter's path - or nothing at all.

test_imports_this_tree in test_hub_tracing.py then asserts that the module
which actually got imported is the one next to this file, because "the tests
passed against a different checkout" is not a result.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
