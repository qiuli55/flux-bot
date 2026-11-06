"""设计稿编译层：把 Luma 画布 JSON 编译成三端只读的 IR（中间表示）。

设计稿 JSON 里到处是「缺省即默认」的隐式语义（type 缺省 text、圆角按类型推、填充按类型推、
渐变只有一个字符串、颜色有 ARGB / rgb() 多种写法）。三端各自解析一遍必然各写一套，
于是出现「手机上是这样、电脑上是另一种」。

编译层把这些全部**展开**成显式值：

* 颜色统一 ``{"hex": "#rrggbb", "alpha": 0-1}``（不透明时省略 alpha）
* 渐变拆成 ``{"kind","angle","center","stops"}``，不再靠字符串猜
* 圆角展开成四角，填充/描边/阴影/文字样式全部补齐
* ``hidden`` 直接剔除、``type`` 统一 trim + lower
* 开关 / 滑杆 / 导航 / 输入框的几何一次算好，渲染端只画不算

渲染端（网页 ``design_render.py``、手机 ``DesignResolve.kt``、桌面 ``DesignCanvas.cs``）
只消费 IR；缺 IR 时再退回本地解析（离线旧数据兜底）。

契约见 ``doc/design-ir.json``，测试见 ``backend/tests/test_design_compile.py``。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from .design_tokens import (
    ACCENT,
    INK,
    INPUT_FILL,
    SHELL_TOP,
    SWITCH_OFF,
    clamp,
    ir_color,
    num,
    parse_color,
    with_alpha,
)

IR_VERSION = 2

#: 文字默认值（与 doc/design-spec.json typography 一致）。
DEFAULT_FONT_SIZE = 13.0
DEFAULT_LINE_HEIGHT = 1.35
DEFAULT_NAV_FONT_SIZE = 10.5
DEFAULT_MAX_LINES = 3
DEFAULT_INPUT_PAD = 12.0
INPUT_PLACEHOLDER_ALPHA = 0.5

#: 导航行高倍数，与移动端 `size * 1.2` 一致。
NAV_LINE_HEIGHT = 1.2

CARD_SHADOW_DY = 6.0
CARD_SHADOW_ALPHA = 0.05
DEFAULT_SHADOW_ALPHA = 0.25

NAV_IDLE_ALPHA = 0.42
NAV_INDICATOR = {"w": 12.0, "h": 2.0, "bottom": 4.0, "alpha": 0.9}
NAV_LABEL_OFFSET_Y = -2.0

SLIDER_TRACK_HEIGHT = 4.0
SLIDER_THUMB = 16.0
SLIDER_RING = 20.0
SLIDER_RING_ALPHA = 0.22
SWITCH_KNOB_INSET = 3.0
PROGRESS_VALUE = 0.5

#: 会被裁掉溢出内容的类型（与 doc/design-spec.json overflow 一致）。
CLIP_TYPES = ("text", "button", "pill", "input", "nav", "badge", "chip", "avatar")

#: 默认文字居中（不跟随 align）的类型。
CENTER_TEXT_TYPES = ("button", "pill", "nav", "badge", "chip", "avatar")

#: 渐变字 / 描边字 / 发光 / 投影的模糊环数量：三端按同一套环参数叠加，
#: 避免各自用不同的高斯实现导致视觉不一致。
FX_RINGS = (
    (0.35, 0.55),
    (0.7, 0.35),
    (1.0, 0.18),
)

_GRADIENT_RE = re.compile(r"^(linear|radial)-gradient\((.*)\)$", re.IGNORECASE)
_ANGLE_RE = re.compile(r"^(-?\d+(?:\.\d+)?)(deg|turn|rad)?$", re.IGNORECASE)
_OFFSET_RE = re.compile(r"^(-?\d+(?:\.\d+)?)%$")
_DIRECTION_ANGLES = {
    "to top": 0.0,
    "to top right": 45.0,
    "to right": 90.0,
    "to bottom right": 135.0,
    "to bottom": 180.0,
    "to bottom left": 225.0,
    "to left": 270.0,
    "to top left": 315.0,
}


# ------------------------------------------------------------------ 基础语义


def flag(raw: Any) -> bool:
    """布尔归一：``"false"`` / ``"0"`` / 空串都算 false（AI 生成的 JSON 常把布尔写成字符串）。"""
    if isinstance(raw, str):
        return raw.strip().lower() not in ("", "0", "false", "no", "none", "null")
    return bool(raw)


def normalize_type(raw: Any) -> str:
    """``trim`` + ``lower``，缺省 text。三端必须走同一实现，否则 ``BUTTON`` 会画成色块。"""
    value = str(raw or "").strip().lower()
    return value or "text"


def default_radius(ntype: str, w: float, h: float) -> float:
    if ntype in ("button", "pill", "input", "slider", "switch", "progress", "badge", "chip"):
        return h / 2.0
    if ntype in ("avatar", "circle"):
        return w / 2.0
    if ntype in ("card", "nav"):
        return 18.0
    return 0.0


def default_fill_color(ntype: str) -> dict[str, Any]:
    """按类型推默认填充色（IR 颜色）。"""
    if ntype in ("button", "switch", "slider", "progress", "badge"):
        color = ir_color(ACCENT)
    elif ntype in ("pill", "decor", "chip", "avatar"):
        color = with_alpha(ir_color(ACCENT), 0.12)
    elif ntype in ("card", "nav"):
        color = ir_color("#FFFFFF")
    elif ntype == "input":
        color = ir_color(INPUT_FILL)
    else:
        color = with_alpha(ir_color(INK), 0.08)
    return color or {"hex": INK}


def default_text_color(ntype: str) -> dict[str, Any]:
    """按类型推默认文字色（IR 颜色）。"""
    if ntype == "badge":
        color = ir_color("#FFFFFF")
    elif ntype in ("chip", "avatar"):
        color = ir_color(ACCENT)
    else:
        color = ir_color(INK)
    return color or ir_color(INK)


def radius_of(ntype: str, w: float, h: float, raw: Any) -> dict[str, float]:
    """四角圆角，显式值优先；负值按 0 处理，上限 ``min(w, h) / 2``。"""
    limit = max(min(w, h) / 2.0, 0.0)
    explicit = num(raw, float("nan"))
    base = explicit if explicit == explicit else default_radius(ntype, w, h)
    base = clamp(base, 0.0, limit)
    corners = {"tl": base, "tr": base, "br": base, "bl": base}
    if isinstance(raw, dict):
        aliases = {"topLeft": "tl", "topRight": "tr", "bottomRight": "br", "bottomLeft": "bl"}
        for key, value in raw.items():
            short = aliases.get(str(key), str(key))
            if short in corners:
                corners[short] = clamp(num(value, 0.0), 0.0, limit)
    elif isinstance(raw, list) and len(raw) == 4:
        corners = {
            key: clamp(num(value, 0.0), 0.0, limit)
            for key, value in zip(("tl", "tr", "br", "bl"), raw, strict=True)
        }
    return {key: round(value, 3) for key, value in corners.items()}


# ------------------------------------------------------------------ 颜色 / 渐变


def _split_top_level(value: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for char in value:
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        if char == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    if current:
        parts.append("".join(current).strip())
    return [part for part in parts if part]


def _angle_value(token: str) -> float | None:
    match = _ANGLE_RE.match(token)
    if match is None:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    unit = (match.group(2) or "deg").lower()
    if unit == "turn":
        value *= 360.0
    elif unit == "rad":
        value = value * 180.0 / 3.141592653589793
    return value % 360.0


def _position_token(token: str) -> float | None:
    """径向渐变圆心坐标：``30%`` / ``left`` / ``center`` 之类 → 0..1。"""
    token = token.strip().lower()
    keywords = {"left": 0.0, "top": 0.0, "center": 0.5, "right": 1.0, "bottom": 1.0}
    if token in keywords:
        return keywords[token]
    if token.endswith("%"):
        try:
            return clamp(float(token[:-1]) / 100.0, 0.0, 1.0)
        except ValueError:
            return None
    return None


def _radial_center(head: str) -> dict[str, float] | None:
    """``circle at 30% 70%`` → ``{"x": .3, "y": .7}``；未给 ``at`` 返回 None。"""
    parts = re.split(r"\bat\b", head, maxsplit=1)
    if len(parts) < 2:
        return None
    tokens = parts[1].split()
    if not tokens:
        return None
    if len(tokens) == 1:
        token = tokens[0].lower()
        value = _position_token(token)
        if value is None:
            return None
        # 单值按 CSS 规则：top/bottom 定 y，其余（left/right/百分比/center）定 x。
        if token in ("top", "bottom"):
            return {"x": 0.5, "y": value}
        return {"x": value, "y": 0.5}
    x = _position_token(tokens[0])
    y = _position_token(tokens[1])
    if x is None or y is None:
        return None
    return {"x": x, "y": y}


def parse_gradient(raw: Any) -> dict[str, Any] | None:
    """解析 CSS 渐变字符串；不支持锥形（conic）与多重背景。

    ``angle`` 与 CSS 同义：0deg 向上、90deg 向右、180deg 向下（顺时针）。
    径向渐变支持圆心位置（``at x y``），渲染端按 ``circle at`` 正圆近似。
    """
    value = raw.strip() if isinstance(raw, str) else ""
    match = _GRADIENT_RE.match(value)
    if match is None:
        return None
    kind = match.group(1).lower()
    segments = _split_top_level(match.group(2))
    if not segments:
        return None

    angle = 180.0
    center = {"x": 0.5, "y": 0.5}
    if kind == "linear":
        head = segments[0].strip().lower()
        resolved = _angle_value(head)
        if resolved is not None:
            angle = resolved
            segments = segments[1:]
        elif head in _DIRECTION_ANGLES:
            angle = _DIRECTION_ANGLES[head]
            segments = segments[1:]
    elif kind == "radial" and segments:
        head = segments[0].strip().lower()
        if "at" in head or head.startswith(("circle", "ellipse", "closest", "farthest")):
            resolved_center = _radial_center(head)
            if resolved_center is not None:
                center = resolved_center
            segments = segments[1:]

    stops: list[dict[str, Any]] = []
    for segment in segments:
        tokens = segment.split()
        if not tokens:
            continue
        color = ir_color(tokens[0])
        if color is None:
            continue
        offset: float | None = None
        for token in tokens[1:]:
            offset_match = _OFFSET_RE.match(token)
            if offset_match:
                offset = clamp(float(offset_match.group(1)) / 100.0, 0.0, 1.0)
                break
        stops.append({"color": color, "offset": offset})
    if not stops:
        return None

    # 未标位置的色标按 CSS 规则在相邻已标位置之间均分。
    first_fixed = next((i for i, stop in enumerate(stops) if stop["offset"] is not None), None)
    if first_fixed is None:
        count = len(stops)
        for index, stop in enumerate(stops):
            stop["offset"] = index / (count - 1) if count > 1 else 0.0
    else:
        for index in range(first_fixed):
            stops[index]["offset"] = 0.0
        last_fixed = max(i for i, stop in enumerate(stops) if stop["offset"] is not None)
        for index in range(last_fixed + 1, len(stops)):
            stops[index]["offset"] = 1.0
        index = first_fixed
        while index <= last_fixed:
            if stops[index]["offset"] is None:
                prev = index - 1
                nxt = index
                while nxt <= last_fixed and stops[nxt]["offset"] is None:
                    nxt += 1
                span = (stops[nxt]["offset"] or 0.0) - (stops[prev]["offset"] or 0.0)
                step = span / (nxt - prev)
                for offset_index in range(index, nxt):
                    stops[offset_index]["offset"] = (stops[prev]["offset"] or 0.0) + step * (
                        offset_index - prev
                    )
                index = nxt
            index += 1

    return {
        "kind": kind,
        "angle": round(angle, 3),
        "center": center,
        "stops": [
            {"color": stop["color"], "offset": round(float(stop["offset"]), 4)} for stop in stops
        ],
    }


def compile_fill(raw: Any, fallback: dict[str, Any] | None) -> dict[str, Any]:
    value = raw.strip() if isinstance(raw, str) else ""
    if value.lower() in ("none", "transparent"):
        return {"kind": "none"}
    gradient = parse_gradient(value)
    if gradient is not None:
        return gradient
    color = ir_color(value, fallback)
    if color is None:
        return {"kind": "none"}
    return {"kind": "solid", "color": color}


def compile_stroke(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        color = ir_color(raw.get("color"))
        if color is None:
            return None
        width = num(raw.get("width"), 1.0)
        return {
            "color": color,
            "width": round(max(width, 0.0), 3),
            "align": str(raw.get("align") or "inside"),
            "dash": [round(num(item, 0.0), 3) for item in (raw.get("dash") or [])],
            "sides": _stroke_sides(raw.get("sides")),
        }
    color = ir_color(raw)
    if color is None:
        return None
    return {"color": color, "width": 1.0, "align": "inside", "dash": [], "sides": None}


def _stroke_sides(raw: Any) -> dict[str, bool] | None:
    if not isinstance(raw, list) or not raw:
        return None
    sides = {"top": False, "right": False, "bottom": False, "left": False}
    for item in raw:
        key = str(item).strip().lower()
        if key in sides:
            sides[key] = True
    return sides


def compile_shadow(raw: Any, fallback_alpha: float = DEFAULT_SHADOW_ALPHA) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    color = ir_color(raw.get("color"), {"hex": "#000000", "alpha": fallback_alpha})
    if color is None:
        return None
    return {
        "dx": round(num(raw.get("dx"), 0.0), 3),
        "dy": round(num(raw.get("dy"), 0.0), 3),
        "blur": round(max(num(raw.get("blur"), 0.0), 0.0), 3),
        "color": color,
        "inset": flag(raw.get("inset")),
    }


#: 阴影模糊的环近似层数（三端共用）。
SHADOW_RINGS = 10


def shadow_layers(shadow: dict[str, Any]) -> list[dict[str, Any]]:
    """IRShadow → 环近似层列表，**大 spread 在前（在下层）**。

    blur=0 退化为单段 spread=0（与历史基准图一致）。透明度合计 ≈ color.alpha。
    """
    color = shadow.get("color") if isinstance(shadow.get("color"), dict) else {}
    base_alpha = num(color.get("alpha"), 1.0)
    if base_alpha <= 0.004:
        return []
    dx = num(shadow.get("dx"), 0.0)
    dy = num(shadow.get("dy"), 0.0)
    blur = max(num(shadow.get("blur"), 0.0), 0.0)
    inset = flag(shadow.get("inset"))
    if blur <= 0.0001:
        return [{"dx": dx, "dy": dy, "spread": 0.0, "alpha": round(base_alpha, 4), "inset": inset}]
    layers: list[dict[str, Any]] = []
    for index in range(SHADOW_RINGS, 0, -1):
        ratio = index / SHADOW_RINGS
        layers.append(
            {
                "dx": dx,
                "dy": dy,
                "spread": round(blur * ratio, 3),
                "alpha": round(base_alpha * 3.0 / SHADOW_RINGS * (1.0 - ratio) ** 2, 4),
                "inset": inset,
            }
        )
    return layers


# ------------------------------------------------------------------ 文字


def font_family(raw: Any) -> str:
    """字族枚举：``system / noto-sans-sc / noto-serif-sc / inter / mono``。"""
    value = re.sub(r"[\s_]+", "-", str(raw or "").strip().lower())
    aliases = {
        "system": "system",
        "": "system",
        "sans": "system",
        "sans-serif": "system",
        "noto-sans": "noto-sans-sc",
        "noto-sans-sc": "noto-sans-sc",
        "source-han-sans": "noto-sans-sc",
        "serif": "noto-serif-sc",
        "noto-serif": "noto-serif-sc",
        "noto-serif-sc": "noto-serif-sc",
        "source-han-serif": "noto-serif-sc",
        "inter": "inter",
        "mono": "mono",
        "jetbrains-mono": "mono",
        "monospace": "mono",
    }
    return aliases.get(value, "system")


def _compile_text_stroke(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    color = ir_color(raw.get("color"))
    if color is None:
        return None
    return {
        "width": round(max(num(raw.get("width"), 1.0), 0.0), 3),
        "color": color,
        "align": str(raw.get("align") or "outside"),
    }


def _compile_fx(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    color = ir_color(raw.get("color"))
    if color is None:
        return None
    return {
        "dx": round(num(raw.get("dx"), 0.0), 3),
        "dy": round(num(raw.get("dy"), 0.0), 3),
        "blur": round(max(num(raw.get("blur"), num(raw.get("radius"), 0.0)), 0.0), 3),
        "intensity": round(clamp(num(raw.get("intensity"), 1.0), 0.0, 1.0), 3),
        "color": color,
        "rings": [[round(radius, 3), round(alpha, 3)] for radius, alpha in FX_RINGS],
    }


def text_style(
    node: dict[str, Any],
    ntype: str,
    w: float = 0.0,
    h: float = 0.0,
    *,
    default_size: float = DEFAULT_FONT_SIZE,
    force_center: bool = False,
) -> dict[str, Any]:
    """文字样式（不判断是否有文本内容）。网页与 IR 共用这一份计算。"""
    align = str(node.get("align") or "").strip().lower()
    if ntype in CENTER_TEXT_TYPES or force_center:
        align = "center"
    if align not in ("left", "center", "right"):
        align = "left"

    color = ir_color(node.get("color"), default_text_color(ntype)) or default_text_color(ntype)

    is_input = ntype == "input"
    pad_left = num(node.get("padLeft"), DEFAULT_INPUT_PAD if is_input else 0.0)
    max_lines = int(clamp(num(node.get("maxLines"), DEFAULT_MAX_LINES), 1.0, 64.0))
    return {
        "value": str(node.get("text") or ""),
        "fontFamily": font_family(node.get("fontFamily")),
        "fontSize": round(max(num(node.get("fontSize"), default_size), 0.0), 3),
        "fontWeight": int(clamp(num(node.get("fontWeight"), 400.0), 1.0, 1000.0)),
        "italic": flag(node.get("italic")),
        "underline": flag(node.get("underline")),
        "strikethrough": flag(node.get("strikethrough")),
        "lineHeight": round(num(node.get("lineHeight"), DEFAULT_LINE_HEIGHT), 3),
        "letterSpacing": round(num(node.get("letterSpacing"), 0.0), 3),
        "align": align,
        "valign": "center",
        "maxLines": max_lines,
        "ellipsis": True,
        "autoFit": flag(node.get("autoFit")),
        "color": color,
        "fill": parse_gradient(node.get("textFill")),
        "stroke": _compile_text_stroke(node.get("textStroke")),
        "glow": _compile_fx(node.get("textGlow")),
        "shadow": _compile_fx(node.get("textShadow")),
        "padLeft": round(max(pad_left, 0.0), 3),
        # 占位符透明度由渲染端单独应用（网页走 ::placeholder opacity），
        # 这里保持 color 全不透明，避免两边相乘变成 0.25。
        "placeholderAlpha": INPUT_PLACEHOLDER_ALPHA if is_input else 1.0,
        "area": {"w": round(w, 3), "h": round(h, 3)},
    }


def compile_text(node: dict[str, Any], ntype: str, w: float, h: float) -> dict[str, Any] | None:
    """有实际文字才输出文本样式块；空白文本返回 None。"""
    value = node.get("text")
    value = "" if value is None else str(value)
    if not value.strip():
        return None
    return text_style(node, ntype, w, h)


# ------------------------------------------------------------------ 控件几何


def _compile_switch(node: dict[str, Any], h: float, fill: dict[str, Any]) -> dict[str, Any]:
    knob = max(h - SWITCH_KNOB_INSET * 2.0, 1.0)
    return {
        "on": flag(node.get("on")),
        "knob": round(knob, 3),
        "inset": SWITCH_KNOB_INSET,
        "knobTop": round((h - knob) / 2.0, 3),
        "onColor": fill.get("color") if fill.get("kind") == "solid" else ir_color(ACCENT),
        "offColor": ir_color(SWITCH_OFF),
    }


def _compile_slider(node: dict[str, Any], w: float, fill: dict[str, Any]) -> dict[str, Any]:
    value = clamp(num(node.get("value"), 0.5), 0.0, 1.0)
    fill_color = fill.get("color") if fill.get("kind") == "solid" else ir_color(ACCENT)
    return {
        "value": round(value, 4),
        "trackColor": ir_color(node.get("track"), SWITCH_OFF) or ir_color(SWITCH_OFF),
        "fillColor": fill_color or ir_color(ACCENT),
        "trackHeight": SLIDER_TRACK_HEIGHT,
        "thumb": SLIDER_THUMB,
        "ring": SLIDER_RING,
        "ringColor": with_alpha(fill_color, SLIDER_RING_ALPHA),
        "thumbX": round(w * value, 3),
    }


def _compile_progress(node: dict[str, Any], fill: dict[str, Any]) -> dict[str, Any]:
    fill_color = fill.get("color") if fill.get("kind") == "solid" else ir_color(ACCENT)
    return {
        "value": round(clamp(num(node.get("value"), PROGRESS_VALUE), 0.0, 1.0), 4),
        "trackColor": ir_color(node.get("track"), SWITCH_OFF) or ir_color(SWITCH_OFF),
        "fillColor": fill_color or ir_color(ACCENT),
    }


def _compile_nav(node: dict[str, Any], w: float) -> dict[str, Any]:
    raw_items = node.get("items")
    items = [str(item) for item in raw_items] if isinstance(raw_items, list) else []
    items = [item for item in items if item.strip()] or ["首页", "任务", "我的"]
    active = int(clamp(num(node.get("active"), 0.0), 0.0, float(len(items) - 1)))
    size = round(num(node.get("fontSize"), DEFAULT_NAV_FONT_SIZE), 3)
    accent = ir_color(ACCENT)
    idle = with_alpha(ir_color(INK), NAV_IDLE_ALPHA) or ir_color(INK)
    return {
        "items": [
            {
                "label": item,
                "active": index == active,
                "fontSize": size,
                "fontWeight": 600 if index == active else 400,
                "color": accent if index == active else idle,
                "lineHeight": NAV_LINE_HEIGHT,
            }
            for index, item in enumerate(items)
        ],
        "cellWidth": round(w / len(items), 3),
        "indicator": dict(NAV_INDICATOR),
        "labelOffsetY": NAV_LABEL_OFFSET_Y,
    }


# ------------------------------------------------------------------ 节点


def compile_node(node: Any, *, parent_type: str | None = None) -> dict[str, Any] | None:
    """单个节点 → IR；返回 None 表示该节点不参与渲染。"""
    if not isinstance(node, dict):
        return None
    if flag(node.get("hidden")):
        return None
    w = num(node.get("w"), 0.0)
    h = num(node.get("h"), 0.0)
    if w <= 0 or h <= 0:
        return None
    opacity = clamp(num(node.get("opacity"), 1.0), 0.0, 1.0)
    if opacity <= 0.01:
        return None

    ntype = normalize_type(node.get("type"))
    raw_fill = node.get("fill")
    if ntype == "text" and not (isinstance(raw_fill, str) and raw_fill.strip()):
        # 纯文字节点没有填充：不能落到「其他类型 = ink@0.08」的默认底色上。
        fill: dict[str, Any] = {"kind": "none"}
    else:
        fill = compile_fill(raw_fill, default_fill_color(ntype))
    corners = radius_of(ntype, w, h, node.get("radius"))
    text = compile_text(node, ntype, w, h)

    shadow = compile_shadow(node.get("shadow"))
    if ntype == "card" and shadow is None:
        shadow = {
            "dx": 0.0,
            "dy": CARD_SHADOW_DY,
            "blur": 0.0,
            "color": {"hex": "#000000", "alpha": CARD_SHADOW_ALPHA},
            "inset": False,
        }

    ir: dict[str, Any] = {
        "id": str(node.get("id") or ""),
        "type": ntype,
        "name": str(node.get("name") or ""),
        "parentType": parent_type,
        "x": round(num(node.get("x"), 0.0), 3),
        "y": round(num(node.get("y"), 0.0), 3),
        "w": round(w, 3),
        "h": round(h, 3),
        "rotation": round(num(node.get("rotation"), 0.0), 3),
        "opacity": round(opacity, 4),
        "radius": corners,
        "fill": fill,
        "stroke": compile_stroke(node.get("stroke")),
        "shadow": shadow,
        "insetShadow": compile_shadow(node.get("insetShadow")),
        "blur": round(max(num(node.get("blur"), 0.0), 0.0), 3),
        "clip": ntype in CLIP_TYPES,
        "ellipse": ntype in ("circle", "avatar") or (ntype == "decor" and abs(w - h) < 0.01),
        "text": text,
        "children": [],
    }
    if ntype == "switch":
        ir["control"] = _compile_switch(node, h, fill)
    elif ntype == "slider":
        ir["control"] = _compile_slider(node, w, fill)
    elif ntype == "progress":
        ir["control"] = _compile_progress(node, fill)
    elif ntype == "nav":
        ir["control"] = _compile_nav(node, w)

    children = node.get("children")
    if isinstance(children, list):
        compiled_children: list[dict[str, Any]] = []
        for child in children:
            compiled = compile_node(child, parent_type=ntype)
            if compiled is not None:
                compiled_children.append(compiled)
        ir["children"] = compiled_children
    return ir


def compile_screen(screen: Any, doc_bg: Any) -> dict[str, Any] | None:
    if not isinstance(screen, dict):
        return None
    w = num(screen.get("w"), 0.0)
    h = num(screen.get("h"), 0.0)
    if w <= 0 or h <= 0:
        return None
    raw_nodes = screen.get("nodes")
    nodes: list[dict[str, Any]] = []
    for node in raw_nodes if isinstance(raw_nodes, list) else []:
        compiled = compile_node(node)
        if compiled is not None:
            nodes.append(compiled)
    return {
        "id": str(screen.get("id") or ""),
        "name": str(screen.get("name") or ""),
        "w": round(w, 3),
        "h": round(h, 3),
        # 画板底色只取文档级 bg，screen.bg 一律忽略（历史数据里两者经常不一致）。
        "bg": doc_bg,
        "nodes": nodes,
    }


def compile_design(design: Any) -> dict[str, Any]:
    """设计稿 JSON → IR。"""
    design = design if isinstance(design, dict) else {}
    bg = ir_color(design.get("bg"), SHELL_TOP) or ir_color(SHELL_TOP)
    raw_screens = design.get("screens")
    screens: list[dict[str, Any]] = []
    for screen in raw_screens if isinstance(raw_screens, list) else []:
        compiled = compile_screen(screen, bg)
        if compiled is not None:
            screens.append(compiled)
    return {
        "version": IR_VERSION,
        "name": str(design.get("name") or "未命名设计稿"),
        "bg": bg,
        "screens": screens,
    }


def iter_nodes(nodes: Iterable[dict[str, Any]]) -> Iterable[dict[str, Any]]:
    """深度优先遍历 IR 节点，供统计 / 校验用。"""
    for node in nodes:
        yield node
        yield from iter_nodes(node.get("children") or [])


__all__ = [
    "IR_VERSION",
    "CENTER_TEXT_TYPES",
    "CLIP_TYPES",
    "FX_RINGS",
    "compile_design",
    "compile_fill",
    "compile_node",
    "compile_shadow",
    "compile_screen",
    "compile_stroke",
    "compile_text",
    "default_fill_color",
    "default_radius",
    "default_text_color",
    "flag",
    "font_family",
    "iter_nodes",
    "normalize_type",
    "parse_gradient",
    "radius_of",
    "shadow_layers",
    "SHADOW_RINGS",
    "text_style",
]

# 供旧的调用方（`from design_compile import parse_color` 之类）继续可用。
_ = parse_color
