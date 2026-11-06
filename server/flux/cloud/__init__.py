"""Flux 账号云接入（B 机 `flux.qiuli55.top`，独立私有仓库）。

本包只做三件事：
- `session`：本地校验云端签发的 HS256 access token、保存/续期本机会话、调用云端接口；
- `uploader`：启动时把「上一轮运行日志」自动上传到云端；
- 账号的权威（注册、登录、注销、后台管理）全在云端，这里不复制一份用户表。
"""

from __future__ import annotations

__all__ = ["session", "uploader"]
