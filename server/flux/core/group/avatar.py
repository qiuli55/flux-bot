"""Bot 头像：形状 × 颜色，不接受上传，前端按同一份枚举确定性渲染。

枚举是前后端共享的契约：这里的取值集合与 `apps/web-dashboard/src/components/group/
BotAvatar.tsx` 的绘制表一一对应。新增形状/颜色必须两边同时改，否则前端会画不出来。
"""

from __future__ import annotations

from flux.errors import ValidationError

#: 形状（14 种）：几何轮廓 + 两只眼睛，保证小尺寸下也能区分。
AVATAR_SHAPES: tuple[str, ...] = (
    "circle",
    "squircle",
    "hexagon",
    "triangle",
    "diamond",
    "star",
    "flower",
    "burst",
    "shield",
    "drop",
    "cloud",
    "gear",
    "egg",
    "blob",
)

#: 颜色（12 种）：两两之间在深色底上也能拉开距离。
AVATAR_COLORS: tuple[str, ...] = (
    "violet",
    "indigo",
    "sky",
    "teal",
    "emerald",
    "lime",
    "amber",
    "orange",
    "rose",
    "fuchsia",
    "slate",
    "cyan",
)

DEFAULT_SHAPE = "circle"
DEFAULT_COLOR = "violet"


def normalize_shape(value: str | None) -> str:
    """形状非法时不回退，直接 422：静默默认会让用户以为选择生效了。"""
    shape = (value or "").strip().lower() or DEFAULT_SHAPE
    if shape not in AVATAR_SHAPES:
        raise ValidationError(
            f"头像形状非法：{value}",
            details={"allowed": list(AVATAR_SHAPES)},
        )
    return shape


def normalize_color(value: str | None) -> str:
    color = (value or "").strip().lower() or DEFAULT_COLOR
    if color not in AVATAR_COLORS:
        raise ValidationError(
            f"头像颜色非法：{value}",
            details={"allowed": list(AVATAR_COLORS)},
        )
    return color


def normalize_avatar(shape: str | None, color: str | None) -> tuple[str, str]:
    """返回 (shape, color)；任一非法都抛 ValidationError。"""
    return normalize_shape(shape), normalize_color(color)
