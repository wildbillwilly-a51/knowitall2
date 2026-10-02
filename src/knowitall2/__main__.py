"""Allow ``python -m knowitall2``.

``serve`` and ``serve-once`` skip the command-line module, so the long-lived
front that agents start loads as little as possible (see ``front``).
"""

import sys

if sys.argv[1:2] == ["serve"]:
    from .front import main
elif sys.argv[1:2] == ["serve-once"]:
    from .mcp_server import run_once as main
else:
    from .cli import main

raise SystemExit(main())
