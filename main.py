#!/usr/bin/env python3
"""DSH MCP Bridge — unified entry point.

    python main.py panel            # control console  http://127.0.0.1:8765
    python main.py http --port 8770 # MCP over Streamable HTTP
    python main.py http start|stop|status
    python main.py doctor [--json]

Implementation lives in ``libs/``; ``python -m libs ...`` is equivalent.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
