"""`python -m flux.cli` 入口。"""

from __future__ import annotations

import sys

from flux.cli.main import main

if __name__ == "__main__":
    sys.exit(main())
