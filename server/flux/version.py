"""应用版本号。

单独成模块是为了让 MCP Server 之类「被 main 间接导入」的组件也能报到同一个版本，
而不必反向 import flux.main（那会形成循环导入）。
"""

from __future__ import annotations

VERSION = "0.1.0"
