"""三端共用的设计绘制基础值：固定色、颜色解析、数值格式化。

编译层（``design_compile``，JSON → IR）与网页渲染层（``design_render``，IR → HTML）
都从这里取默认色和颜色语法，避免同一套规则出现第二份实现。

颜色约定：

* 输入支持 ``#rgb`` / ``#rgba`` / ``#rrggbb`` / ``#aarrggbb``（**ARGB**，Android 顺序）、
  ``rgb()`` / ``rgba()`` / ``hsl()`` / ``hsla()`` / CSS 具名色 / ``transparent`` / ``none``
* IR 内部统一 ``{"hex": "#rrggbb", "alpha": 0-1}``，``alpha`` 为 1 时省略
* 输出 CSS 时转回 ``#rrggbb`` / ``rgba(r,g,b,a)``
"""

from __future__ import annotations

import re
from typing import Any

#: 与移动端 V2Colors 对齐：绘制默认值都基于这几个色（见 DesignSpec.kt defaultFill）。
ACCENT = "#0891B2"
INK = "#0F172A"
SHELL_TOP = "#F4F6F9"
SWITCH_OFF = "#E2E8F0"
INPUT_FILL = "#F8FAFC"

_HEX_RE = re.compile(r"^#([0-9a-fA-F]{3,8})$")
_RGB_RE = re.compile(r"^rgba?\(([^)]*)\)$", re.IGNORECASE)
_HSL_RE = re.compile(r"^hsla?\(([^)]*)\)$", re.IGNORECASE)
_COLOR_TOKEN_RE = re.compile(r"#[0-9a-fA-F]{3,8}|rgba?\([^)]*\)|hsla?\([^)]*\)")

#: CSS 标准具名色（含 transparent）。IR 编译后三端都不再需要这张表。
NAMED_COLORS = {
    "aliceblue": "#f0f8ff",
    "antiquewhite": "#faebd7",
    "aqua": "#00ffff",
    "aquamarine": "#7fffd4",
    "azure": "#f0ffff",
    "beige": "#f5f5dc",
    "bisque": "#ffe4c4",
    "black": "#000000",
    "blanchedalmond": "#ffebcd",
    "blue": "#0000ff",
    "blueviolet": "#8a2be2",
    "brown": "#a52a2a",
    "burlywood": "#deb887",
    "cadetblue": "#5f9ea0",
    "chartreuse": "#7fff00",
    "chocolate": "#d2691e",
    "coral": "#ff7f50",
    "cornflowerblue": "#6495ed",
    "cornsilk": "#fff8dc",
    "crimson": "#dc143c",
    "cyan": "#00ffff",
    "darkblue": "#00008b",
    "darkcyan": "#008b8b",
    "darkgoldenrod": "#b8860b",
    "darkgray": "#a9a9a9",
    "darkgreen": "#006400",
    "darkgrey": "#a9a9a9",
    "darkkhaki": "#bdb76b",
    "darkmagenta": "#8b008b",
    "darkolivegreen": "#556b2f",
    "darkorange": "#ff8c00",
    "darkorchid": "#9932cc",
    "darkred": "#8b0000",
    "darksalmon": "#e9967a",
    "darkseagreen": "#8fbc8f",
    "darkslateblue": "#483d8b",
    "darkslategray": "#2f4f4f",
    "darkslategrey": "#2f4f4f",
    "darkturquoise": "#00ced1",
    "darkviolet": "#9400d3",
    "deeppink": "#ff1493",
    "deepskyblue": "#00bfff",
    "dimgray": "#696969",
    "dimgrey": "#696969",
    "dodgerblue": "#1e90ff",
    "firebrick": "#b22222",
    "floralwhite": "#fffaf0",
    "forestgreen": "#228b22",
    "fuchsia": "#ff00ff",
    "gainsboro": "#dcdcdc",
    "ghostwhite": "#f8f8ff",
    "gold": "#ffd700",
    "goldenrod": "#daa520",
    "gray": "#808080",
    "green": "#008000",
    "greenyellow": "#adff2f",
    "grey": "#808080",
    "honeydew": "#f0fff0",
    "hotpink": "#ff69b4",
    "indianred": "#cd5c5c",
    "indigo": "#4b0082",
    "ivory": "#fffff0",
    "khaki": "#f0e68c",
    "lavender": "#e6e6fa",
    "lavenderblush": "#fff0f5",
    "lawngreen": "#7cfc00",
    "lemonchiffon": "#fffacd",
    "lightblue": "#add8e6",
    "lightcoral": "#f08080",
    "lightcyan": "#e0ffff",
    "lightgoldenrodyellow": "#fafad2",
    "lightgray": "#d3d3d3",
    "lightgreen": "#90ee90",
    "lightgrey": "#d3d3d3",
    "lightpink": "#ffb6c1",
    "lightsalmon": "#ffa07a",
    "lightseagreen": "#20b2aa",
    "lightskyblue": "#87cefa",
    "lightslategray": "#778899",
    "lightslategrey": "#778899",
    "lightsteelblue": "#b0c4de",
    "lightyellow": "#ffffe0",
    "lime": "#00ff00",
    "limegreen": "#32cd32",
    "linen": "#faf0e6",
    "magenta": "#ff00ff",
    "maroon": "#800000",
    "mediumaquamarine": "#66cdaa",
    "mediumblue": "#0000cd",
    "mediumorchid": "#ba55d3",
    "mediumpurple": "#9370db",
    "mediumseagreen": "#3cb371",
    "mediumslateblue": "#7b68ee",
    "mediumspringgreen": "#00fa9a",
    "mediumturquoise": "#48d1cc",
    "mediumvioletred": "#c71585",
    "midnightblue": "#191970",
    "mintcream": "#f5fffa",
    "mistyrose": "#ffe4e1",
    "moccasin": "#ffe4b5",
    "navajowhite": "#ffdead",
    "navy": "#000080",
    "oldlace": "#fdf5e6",
    "olive": "#808000",
    "olivedrab": "#6b8e23",
    "orange": "#ffa500",
    "orangered": "#ff4500",
    "orchid": "#da70d6",
    "palegoldenrod": "#eee8aa",
    "palegreen": "#98fb98",
    "paleturquoise": "#afeeee",
    "palevioletred": "#db7093",
    "papayawhip": "#ffefd5",
    "peachpuff": "#ffdab9",
    "peru": "#cd853f",
    "pink": "#ffc0cb",
    "plum": "#dda0dd",
    "powderblue": "#b0e0e6",
    "purple": "#800080",
    "rebeccapurple": "#663399",
    "red": "#ff0000",
    "rosybrown": "#bc8f8f",
    "royalblue": "#4169e1",
    "saddlebrown": "#8b4513",
    "salmon": "#fa8072",
    "sandybrown": "#f4a460",
    "seagreen": "#2e8b57",
    "seashell": "#fff5ee",
    "sienna": "#a0522d",
    "silver": "#c0c0c0",
    "skyblue": "#87ceeb",
    "slateblue": "#6a5acd",
    "slategray": "#708090",
    "slategrey": "#708090",
    "snow": "#fffafa",
    "springgreen": "#00ff7f",
    "steelblue": "#4682b4",
    "tan": "#d2b48c",
    "teal": "#008080",
    "thistle": "#d8bfd8",
    "tomato": "#ff6347",
    "turquoise": "#40e0d0",
    "violet": "#ee82ee",
    "wheat": "#f5deb3",
    "white": "#ffffff",
    "whitesmoke": "#f5f5f5",
    "yellow": "#ffff00",
    "yellowgreen": "#9acd32",
    "transparent": "#00000000",
}


# ------------------------------------------------------------------ 数值


def num(value: Any, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def num_str(value: float) -> str:
    text = f"{round(value, 3):.3f}".rstrip("0").rstrip(".")
    return text or "0"


def px(value: float) -> str:
    return f"{num_str(value)}px"


# ------------------------------------------------------------------ 颜色解析


def _split_args(body: str) -> list[str]:
    return [part for part in re.split(r"[,\s/]+", body.strip()) if part]


def _parse_channel(token: str, scale: float) -> float | None:
    try:
        if token.endswith("%"):
            return clamp(float(token[:-1]) / 100.0 * scale, 0.0, scale)
        return clamp(float(token), 0.0, scale)
    except ValueError:
        return None


def _parse_alpha(token: str) -> float | None:
    try:
        if token.endswith("%"):
            return clamp(float(token[:-1]) / 100.0, 0.0, 1.0)
        return clamp(float(token), 0.0, 1.0)
    except ValueError:
        return None


def _hsl_to_rgb(hue: float, saturation: float, lightness: float) -> tuple[int, int, int]:
    hue = (hue % 360.0) / 360.0
    saturation = clamp(saturation, 0.0, 1.0)
    lightness = clamp(lightness, 0.0, 1.0)
    if saturation == 0:
        value = int(round(lightness * 255))
        return (value, value, value)

    def channel(p: float, q: float, t: float) -> float:
        t = t % 1.0
        if t < 1 / 6:
            return p + (q - p) * 6 * t
        if t < 1 / 2:
            return q
        if t < 2 / 3:
            return p + (q - p) * (2 / 3 - t) * 6
        return p

    if lightness < 0.5:
        q = lightness * (1 + saturation)
    else:
        q = lightness + saturation - lightness * saturation
    p = 2 * lightness - q
    return (
        int(round(channel(p, q, hue + 1 / 3) * 255)),
        int(round(channel(p, q, hue) * 255)),
        int(round(channel(p, q, hue - 1 / 3) * 255)),
    )


def parse_color(value: Any) -> tuple[int, int, int, int] | None:
    """任意颜色写法 → ``(r, g, b, a)``，``a`` 取 0..255；无法解析返回 ``None``。

    ``#aarrggbb`` 按 Android 的 **ARGB** 顺序解析（与 ``Color.parseColor`` 一致）。
    """
    if not isinstance(value, str):
        return None
    raw = value.strip().lower()
    if not raw:
        return None
    named = NAMED_COLORS.get(raw)
    if named is not None:
        raw = named

    match = _HEX_RE.match(raw)
    if match:
        digits = match.group(1)
        if len(digits) in (3, 4):
            digits = "".join(char * 2 for char in digits)
        try:
            if len(digits) == 8:
                return (
                    int(digits[2:4], 16),
                    int(digits[4:6], 16),
                    int(digits[6:8], 16),
                    int(digits[0:2], 16),
                )
            if len(digits) == 6:
                return (
                    int(digits[0:2], 16),
                    int(digits[2:4], 16),
                    int(digits[4:6], 16),
                    255,
                )
        except ValueError:
            return None
        return None

    match = _RGB_RE.match(raw)
    if match:
        parts = _split_args(match.group(1))
        if len(parts) < 3:
            return None
        r = _parse_channel(parts[0], 255.0)
        g = _parse_channel(parts[1], 255.0)
        b = _parse_channel(parts[2], 255.0)
        if r is None or g is None or b is None:
            return None
        alpha = 1.0
        if len(parts) >= 4:
            parsed = _parse_alpha(parts[3])
            if parsed is None:
                return None
            alpha = parsed
        return (int(round(r)), int(round(g)), int(round(b)), int(round(alpha * 255)))

    match = _HSL_RE.match(raw)
    if match:
        parts = _split_args(match.group(1))
        if len(parts) < 3:
            return None
        try:
            hue = float(parts[0].rstrip("deg"))
        except ValueError:
            return None
        saturation = _parse_channel(parts[1], 1.0)
        lightness = _parse_channel(parts[2], 1.0)
        if saturation is None or lightness is None:
            return None
        alpha = 1.0
        if len(parts) >= 4:
            parsed = _parse_alpha(parts[3])
            if parsed is None:
                return None
            alpha = parsed
        r, g, b = _hsl_to_rgb(hue, saturation, lightness)
        return (r, g, b, int(round(alpha * 255)))

    return None


def rgba_hex(value: str) -> str:
    """规范化为 ``#rrggbb``（小写）；无法解析返回空串。"""
    parsed = parse_color(value)
    if parsed is None:
        return ""
    return f"#{parsed[0]:02x}{parsed[1]:02x}{parsed[2]:02x}"


def alpha_of(value: str) -> float:
    parsed = parse_color(value)
    return 1.0 if parsed is None else parsed[3] / 255.0


def css_color(parsed: tuple[int, int, int, int] | str) -> str:
    """``(r,g,b,a)`` 或颜色字符串 → CSS 颜色。"""
    if isinstance(parsed, str):
        value = parse_color(parsed)
        if value is None:
            return parsed
        parsed = value
    r, g, b, a = parsed
    if a >= 255:
        return f"#{r:02x}{g:02x}{b:02x}"
    return f"rgba({r},{g},{b},{a / 255:.3f})"


def alpha_css(color: str, alpha: float) -> str:
    """覆盖透明度（不与原 alpha 相乘）。"""
    parsed = parse_color(color)
    if parsed is None:
        return color
    r, g, b, _ = parsed
    return f"rgba({r},{g},{b},{alpha:.3f})"


def color_css(raw: Any, fallback: str) -> str:
    """等价于旧 ``_single_color``：单色取色、渐变取最后色标、失败用 fallback。"""
    value = raw.strip() if isinstance(raw, str) else ""
    if not value:
        return fallback
    parsed = parse_color(value)
    if parsed is not None:
        return css_color(parsed)
    if "gradient" in value.lower():
        tokens = _COLOR_TOKEN_RE.findall(value)
        if tokens:
            return color_css(tokens[-1], fallback)
    return fallback


# ------------------------------------------------------------------ IR 颜色


def ir_color(raw: Any, fallback: Any = None) -> dict[str, Any] | None:
    """任意颜色写法 → ``{"hex": "#rrggbb", "alpha": 0-1}``（不透明时省略 alpha）。"""
    parsed = parse_color(raw) if isinstance(raw, str) else None
    if parsed is None:
        if fallback is None:
            return None
        return ir_color(fallback) if not isinstance(fallback, dict) else dict(fallback)
    r, g, b, a = parsed
    color: dict[str, Any] = {"hex": f"#{r:02x}{g:02x}{b:02x}"}
    if a < 255:
        color["alpha"] = round(a / 255.0, 6)
    return color


def with_alpha(color: dict[str, Any] | None, alpha: float) -> dict[str, Any] | None:
    """返回覆盖透明度后的新颜色（不与原 alpha 相乘，与 CSS ``rgba()`` 一致）。"""
    if color is None:
        return None
    result = {"hex": color["hex"]}
    value = round(clamp(alpha, 0.0, 1.0), 6)
    if value < 1.0:
        result["alpha"] = value
    return result


def ir_color_css(color: dict[str, Any] | None, fallback: str = "transparent") -> str:
    """IR 颜色 → CSS 颜色。"""
    if not isinstance(color, dict) or not color.get("hex"):
        return fallback
    alpha = clamp(num(color.get("alpha"), 1.0), 0.0, 1.0)
    if alpha >= 1.0:
        return str(color["hex"])
    parsed = parse_color(str(color["hex"]))
    if parsed is None:
        return fallback
    return f"rgba({parsed[0]},{parsed[1]},{parsed[2]},{alpha:.3f})"


def gradient_token_colors(value: str) -> list[str]:
    """提取渐变字符串里的颜色 token（供降级单色使用）。"""
    return _COLOR_TOKEN_RE.findall(value) if isinstance(value, str) else []
