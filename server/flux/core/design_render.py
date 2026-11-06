"""设计稿 IR → 单文件可交互 HTML（App 内网页预览）。

渲染分两层：

* ``design_compile`` 把 Luma 画布 JSON 编译成 IR（默认值全部展开）
* 本模块只把 IR 翻成 HTML/CSS，不再做任何"缺省推断"

坐标：画板与节点都用设计单位（≈dp），节点坐标相对父节点；整块画板用 CSS transform
等比缩放到视口内，交互命中直接用元素自身矩形换算，不受缩放影响。

网页是跨端一致性的**权威基准**：手机 Compose 画布与桌面 Avalonia 画布都以这里
的输出为准，三者共用 ``doc/design-spec.json`` + ``doc/design-ir.json`` 两份契约。
"""

from __future__ import annotations

import html
import json
from typing import Any

from .design_compile import (
    DEFAULT_FONT_SIZE,
    compile_design,
    compile_fill,
    compile_node,
    default_fill_color,
    default_radius,
    normalize_type,
    shadow_layers,
    text_style,
)
from .design_tokens import (
    ACCENT,
    INK,
    INPUT_FILL,
    SHELL_TOP,
    SWITCH_OFF,
    alpha_css,
    color_css,
    css_color,
    ir_color_css,
    rgba_hex,
)
from .design_tokens import (
    clamp as _clamp,
)
from .design_tokens import (
    num as _num,
)
from .design_tokens import (
    num_str as _num_str,
)
from .design_tokens import (
    px as _px,
)

#: 演示里的"返回"类文案；命中即回上一屏（与 demoNextScreen 一致）。
_BACK_WORDS = ("返回", "取消", "关闭", "退出")

#: 字族枚举 → CSS 字体栈。三端各自注册同名回退链，保证同一份 IR 落到同样的字形。
_FONT_STACKS = {
    "system": (
        '-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",'
        '"Hiragino Sans GB","Microsoft YaHei",sans-serif'
    ),
    "noto-sans-sc": '"Noto Sans SC","Noto Sans CJK SC","Source Han Sans SC","PingFang SC",sans-serif',
    "noto-serif-sc": '"Noto Serif SC","Noto Serif CJK SC","Source Han Serif SC","Songti SC",serif',
    "inter": '"Inter","Helvetica Neue",Arial,sans-serif',
    "mono": '"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace',
}


def render_design_html(design: dict[str, Any], *, page_title: str | None = None) -> str:
    """把一份设计稿渲染成自包含的单文件 HTML（无外部依赖，可直接 loadDataWithBaseURL）。"""
    return render_ir_html(compile_design(design), page_title=page_title)


def render_ir_html(ir: dict[str, Any], *, page_title: str | None = None) -> str:
    """把编译好的 IR 渲染成 HTML。调用方已有 IR 时走这里，省一次编译。"""
    ir = ir if isinstance(ir, dict) else {}
    raw_screens = ir.get("screens")
    screens = [item for item in (raw_screens or []) if isinstance(item, dict)]
    name = str(ir.get("name") or "未命名设计稿")
    bg = ir_color_css(ir.get("bg"), SHELL_TOP)
    title = (page_title or name).strip() or "设计稿演示"

    meta = [
        {"id": str(screen.get("id") or ""), "name": str(screen.get("name") or "")}
        for screen in screens
    ]

    if screens:
        devices = "\n".join(_screen_html(screen, bg) for screen in screens)
        body_main = (
            '<main class="stage"><div class="holder" id="holder">\n'
            f"{devices}\n"
            "</div></main>\n"
            '<footer class="foot">\n'
            '  <div class="hint">点按钮切屏 · 拖动滑杆 · 点开关切换</div>\n'
            '  <div class="pager">\n'
            '    <button class="pg" id="prev" type="button" aria-label="上一屏">&lsaquo;</button>\n'
            '    <div class="dots" id="dots"></div>\n'
            '    <button class="pg" id="next" type="button" aria-label="下一屏">&rsaquo;</button>\n'
            "  </div>\n"
            "</footer>"
        )
        header_count = f'<div class="count" id="count">1 / {len(screens)}</div>'
    else:
        body_main = '<main class="stage"><div class="empty">这份设计稿还没有画板</div></main>'
        header_count = ""

    screens_json = json.dumps(meta, ensure_ascii=False).replace("</", "<\\/")

    return _TEMPLATE.format(
        title=html.escape(title),
        name=html.escape(name),
        count=header_count,
        main=body_main,
        screens_json=screens_json,
        css=_CSS,
        back_words=json.dumps(list(_BACK_WORDS), ensure_ascii=False),
    )


# ------------------------------------------------------------------ 画板 / 节点


def _screen_html(screen: dict[str, Any], bg: str) -> str:
    w = _num(screen.get("w"), 0.0)
    h = _num(screen.get("h"), 0.0)
    if w <= 0 or h <= 0:
        return ""
    inner = "\n".join(_ir_node_html(node) for node in screen.get("nodes") or [])
    return (
        f'<section class="device" data-w="{_num_str(w)}" data-h="{_num_str(h)}" '
        f'data-id="{html.escape(str(screen.get("id") or ""), quote=True)}" '
        f'data-name="{html.escape(str(screen.get("name") or ""), quote=True)}" '
        f'style="width:{_px(w)};height:{_px(h)}">\n'
        f'<div class="screen" style="background:{bg}">\n{inner}\n</div>\n'
        "</section>"
    )


def _node_html(node: dict[str, Any]) -> str:
    """单节点 → HTML（旧调用方入口，内部走编译层，保证与整稿渲染同一条路径）。"""
    compiled = compile_node(node)
    return "" if compiled is None else _ir_node_html(compiled)


def _ir_node_html(node: dict[str, Any]) -> str:
    ntype = str(node.get("type") or "text")
    w = _num(node.get("w"), 0.0)
    h = _num(node.get("h"), 0.0)
    style: list[str] = [
        f"left:{_px(_num(node.get('x'), 0.0))}",
        f"top:{_px(_num(node.get('y'), 0.0))}",
        f"width:{_px(w)}",
        f"height:{_px(h)}",
    ]
    opacity = _clamp(_num(node.get("opacity"), 1.0), 0.0, 1.0)
    if opacity < 1:
        style.append(f"opacity:{_num_str(opacity)}")
    rotation = _num(node.get("rotation"), 0.0)
    if abs(rotation) > 0.01:
        # transform-origin 默认 50% 50%，与 Compose rotate(pivot = center) 一致。
        style.append(f"transform:rotate({_num_str(rotation)}deg)")
    blur = _num(node.get("blur"), 0.0)
    if blur > 0:
        style.append(f"filter:blur({_px(blur)})")

    classes = ["n"]
    attrs = {
        "data-id": str(node.get("id") or ""),
        "data-type": ntype,
        "data-name": str(node.get("name") or ""),
        "data-text": _text_value(node.get("text")),
    }
    children_html = "".join(_ir_node_html(child) for child in node.get("children") or [])
    fill = node.get("fill") if isinstance(node.get("fill"), dict) else {"kind": "none"}
    fill_css = _fill_css(fill)
    radius_css = _radius_css(node.get("radius"), node.get("ellipse") is True)
    stroke_css = _stroke_css(node.get("stroke"))
    shadow_css = _shadow_css(node.get("shadow"))
    inset_shadow_css = _shadow_css(node.get("insetShadow"), inset=True)

    def surface(*, background: bool = True, radius: bool = True) -> None:
        if background and fill_css:
            style.append(f"background:{fill_css}")
        if radius:
            style.append(radius_css)
        if stroke_css:
            style.append(stroke_css)
        if shadow_css:
            style.append(shadow_css)
        if inset_shadow_css:
            style.append(inset_shadow_css)

    if ntype == "text":
        classes.append("t-text")
        text_html = _text_span(node.get("text"))
        if fill.get("kind") != "none":
            surface()
        return _element(classes, style, attrs, text_html + children_html)

    if ntype in ("button", "pill"):
        surface()
        attrs["role"] = "button"
        attrs["tabindex"] = "0"
        return _element(
            ["n", f"t-{ntype}"], style, attrs, _text_span(node.get("text")) + children_html
        )

    if ntype == "slider":
        control = node.get("control") or {}
        track = ir_color_css(control.get("trackColor"), SWITCH_OFF)
        fill_color = ir_color_css(control.get("fillColor"), ACCENT)
        ring = ir_color_css(control.get("ringColor"), alpha_css(ACCENT, 0.22))
        style.append(f"--v:{_num_str(_num(control.get('value'), 0.5))}")
        surface(background=False)
        inner = (
            f'<div class="sl-track" style="background:{track}"></div>'
            f'<div class="sl-fill" style="background:{fill_color}"></div>'
            '<div class="sl-thumb"></div>'
            f'<div class="sl-ring" style="background:{ring}"></div>'
        )
        return _element(["n", "t-slider"], style, attrs, inner + children_html)

    if ntype == "switch":
        control = node.get("control") or {}
        style.append("--c-on:" + ir_color_css(control.get("onColor"), ACCENT))
        style.append(f"--knob:{_px(_num(control.get('knob'), max(h - 6.0, 1.0)))}")
        style.append(
            f"--knob-top:{_px(_num(control.get('knobTop'), (h - max(h - 6.0, 1.0)) / 2.0))}"
        )
        base = [item for item in style if not item.startswith("background:")]
        base.append(_radius_css(node.get("radius"), False))
        if stroke_css:
            base.append(stroke_css)
        if shadow_css:
            base.append(shadow_css)
        on_class = ["on"] if control.get("on") is True else []
        return _element(
            ["n", "t-switch"] + on_class, base, attrs, '<div class="sw-knob"></div>' + children_html
        )

    if ntype == "nav":
        control = node.get("control") or {}
        cells = []
        for item in control.get("items") or []:
            on = item.get("active") is True
            cell_style = (
                f"font-size:{_px(_num(item.get('fontSize'), 10.5))};"
                f"font-weight:{int(_num(item.get('fontWeight'), 400))};"
                f"line-height:{_num_str(_num(item.get('lineHeight'), 1.2))};"
                f"color:{ir_color_css(item.get('color'), ACCENT if on else INK)}"
            )
            cells.append(
                f'<div class="nav-cell{" on" if on else ""}">'
                f'<span style="{cell_style}">{html.escape(str(item.get("label") or ""))}</span>'
                f'<i style="background:{ir_color_css(item.get("color"), ACCENT)}"></i></div>'
            )
        attrs["data-items"] = json.dumps(
            [str(item.get("label") or "") for item in control.get("items") or []],
            ensure_ascii=False,
        )
        surface()
        return _element(["n", "t-nav"], style, attrs, "".join(cells) + children_html)

    if ntype == "input":
        text = node.get("text") or {}
        placeholder_alpha = _num(text.get("placeholderAlpha"), 0.5)
        attrs["data-placeholder-alpha"] = _num_str(placeholder_alpha)
        surface()
        inner = (
            f'<input type="text" autocomplete="off" '
            f'placeholder="{html.escape(_text_value(text), quote=True)}" '
            f'style="{_text_css_from(text)}padding-left:{_px(_num(text.get("padLeft"), 12.0))}">'
        )
        return _element(["n", "t-input"], style, attrs, inner + children_html)

    if ntype == "progress":
        control = node.get("control") or {}
        track = ir_color_css(control.get("trackColor"), SWITCH_OFF)
        fill_color = ir_color_css(control.get("fillColor"), ACCENT)
        style.append(f"--v:{_num_str(_num(control.get('value'), 0.5))}")
        surface(background=False)
        inner = (
            f'<div class="pg-track" style="background:{track}"></div>'
            f'<div class="pg-fill" style="background:{fill_color}"></div>'
        )
        return _element(["n", "t-progress"], style, attrs, inner + children_html)

    if ntype in ("badge", "chip", "avatar"):
        surface()
        return _element(
            ["n", f"t-{ntype}"], style, attrs, _text_span(node.get("text")) + children_html
        )

    # card / circle / decor / divider / 未知类型：都是"底色圆角矩形"（圆或方形另算）
    surface()
    if ntype == "circle":
        classes.append("t-circle")
    elif ntype == "decor":
        classes.append("t-decor")
    elif ntype == "card":
        classes.append("t-card")
    return _element(classes, style, attrs, children_html)


def _text_value(block: Any) -> str:
    if isinstance(block, dict):
        return str(block.get("value") or "")
    return ""


def _text_span(block: Any) -> str:
    """文字块：垂直居中、按 maxLines 省略、可选渐变/描边/发光/投影。"""
    if not isinstance(block, dict) or not str(block.get("value") or "").strip():
        return ""
    style = _text_css_from(block)
    classes = ["tx"]
    if isinstance(block.get("fill"), dict):
        style += f"background-image:{_gradient_css(block['fill'])};"
        classes.append("tx-fill")
    stroke = block.get("stroke")
    if isinstance(stroke, dict) and _num(stroke.get("width"), 0.0) > 0:
        width = _num(stroke.get("width"), 1.0)
        # -webkit-text-stroke 以字形轮廓为中线；fill 覆盖内侧一半，
        # 因此 align=outside 时要把线宽翻倍，才能与 Compose 的"外描边"占位相同。
        if str(stroke.get("align") or "outside").lower() != "inside":
            width *= 2.0
        style += f"-webkit-text-stroke:{_px(width)} {ir_color_css(stroke.get('color'), INK)};"
    shadows = _text_shadow_css(block)
    if shadows:
        style += f"text-shadow:{','.join(shadows)};"
    auto_fit = ""
    if block.get("autoFit") is True:
        classes.append("tx-autofit")
        auto_fit = f' data-fit="{_num_str(_num(block.get("fontSize"), DEFAULT_FONT_SIZE))}"'
    return (
        f'<span class="{" ".join(classes)}" style="{html.escape(style, quote=True)}"{auto_fit}>'
        f"{html.escape(str(block.get('value') or ''))}</span>"
    )


def _text_shadow_css(block: dict[str, Any]) -> list[str]:
    """发光/投影 → 固定环叠加的 text-shadow，三端用同一套环参数。"""
    parts: list[str] = []
    for key, is_glow in (("glow", True), ("shadow", False)):
        effect = block.get(key)
        if not isinstance(effect, dict):
            continue
        color = effect.get("color")
        base_alpha = _num(color.get("alpha"), 1.0) if isinstance(color, dict) else 1.0
        intensity = _num(effect.get("intensity"), 1.0)
        dx = _num(effect.get("dx"), 0.0)
        dy = _num(effect.get("dy"), 0.0)
        blur = _num(effect.get("blur"), 0.0)
        rings = effect.get("rings") or [[1.0, 1.0]]
        for radius, ring_alpha in rings:
            alpha = _clamp(base_alpha * intensity * float(ring_alpha), 0.0, 1.0)
            if alpha <= 0.004:
                continue
            ox = 0.0 if is_glow else dx
            oy = 0.0 if is_glow else dy
            parts.append(
                f"{_num_str(ox)}px {_num_str(oy)}px {_px(blur * float(radius))} "
                f"{_rgba_css(color, alpha)}"
            )
    return parts


def _rgba_css(color: Any, alpha: float) -> str:
    if not isinstance(color, dict):
        return alpha_css("#000000", alpha)
    return alpha_css(str(color.get("hex") or "#000000"), alpha)


def _element(classes: list[str], style: list[str], attrs: dict[str, str], inner: str) -> str:
    attr_text = "".join(
        f' {key}="{html.escape(value, quote=True)}"' for key, value in attrs.items()
    )
    return (
        f'<div class="{html.escape(" ".join(classes), quote=True)}"'
        f' style="{html.escape(";".join(style), quote=True)}"{attr_text}>{inner}</div>'
    )


# ------------------------------------------------------------------ 样式计算


def _text_css(
    node: dict[str, Any], default_size: float = DEFAULT_FONT_SIZE, *, force_center: bool = False
) -> str:
    """文字 CSS（旧调用方入口，走编译层的同一份样式计算）。"""
    node = node if isinstance(node, dict) else {}
    style = text_style(
        node,
        normalize_type(node.get("type")),
        default_size=default_size,
        force_center=force_center,
    )
    return _text_css_from(style)


def _text_css_from(block: dict[str, Any]) -> str:
    block = block if isinstance(block, dict) else {}
    size = _num(block.get("fontSize"), DEFAULT_FONT_SIZE)
    weight = int(_clamp(_num(block.get("fontWeight"), 400.0), 1.0, 1000.0))
    line_height = _num(block.get("lineHeight"), 1.35)
    spacing = _num(block.get("letterSpacing"), 0.0)
    align = str(block.get("align") or "left")
    css = (
        f"font-size:{_px(size)};"
        f"font-weight:{weight};"
        f"line-height:{_num_str(line_height)};"
        f"letter-spacing:{_px(spacing)};"
        f"text-align:{align if align in ('center', 'right') else 'left'};"
        f"color:{ir_color_css(block.get('color'), INK)};"
    )
    family = _FONT_STACKS.get(str(block.get("fontFamily") or "system"), _FONT_STACKS["system"])
    css += f"font-family:{family};"
    decorations = []
    if block.get("underline") is True:
        decorations.append("underline")
    if block.get("strikethrough") is True:
        decorations.append("line-through")
    if decorations:
        css += f"text-decoration:{' '.join(decorations)};"
    if block.get("italic") is True:
        css += "font-style:italic;"
    max_lines = int(_clamp(_num(block.get("maxLines"), 3), 1.0, 64.0))
    css += f"-webkit-line-clamp:{max_lines};"
    return css


def _radius_css(radius: Any, ellipse: bool) -> str:
    if ellipse:
        return "border-radius:50%"
    if not isinstance(radius, dict):
        return "border-radius:0"
    corners = [round(_num(radius.get(key), 0.0), 3) for key in ("tl", "tr", "br", "bl")]
    if len(set(corners)) == 1:
        return f"border-radius:{_px(corners[0])}"
    return "border-radius:" + " ".join(_px(value) for value in corners)


def _fill_css(fill: dict[str, Any]) -> str | None:
    kind = str(fill.get("kind") or "none")
    if kind == "solid":
        return ir_color_css(fill.get("color"), "transparent")
    if kind in ("linear", "radial"):
        return _gradient_css(fill)
    return None


def _gradient_css(gradient: dict[str, Any]) -> str:
    stops = []
    for index, stop in enumerate(gradient.get("stops") or []):
        color = ir_color_css(stop.get("color"), "transparent")
        offset = _clamp(_num(stop.get("offset"), 0.0), 0.0, 1.0)
        if (index == 0 and offset <= 0.0001) or (
            index == len(gradient.get("stops") or []) - 1 and offset >= 0.9999
        ):
            stops.append(color)
        else:
            stops.append(f"{color} {round(offset * 100, 3)}%")
    body = ", ".join(stops)
    if str(gradient.get("kind")) == "radial":
        center = gradient.get("center") or {}
        cx = round(_num(center.get("x"), 0.5) * 100, 3)
        cy = round(_num(center.get("y"), 0.5) * 100, 3)
        return f"radial-gradient(circle at {cx}% {cy}%, {body})"
    return f"linear-gradient({_num_str(_num(gradient.get('angle'), 180.0))}deg, {body})"


def _stroke_css(stroke: Any) -> str | None:
    if not isinstance(stroke, dict):
        return None
    width = _num(stroke.get("width"), 0.0)
    if width <= 0:
        return None
    color = ir_color_css(stroke.get("color"), INK)
    style = "dashed" if stroke.get("dash") else "solid"
    sides = stroke.get("sides")
    if isinstance(sides, dict) and any(sides.values()):
        return ";".join(
            f"border-{side}:{_px(width)} {style} {color}"
            for side in ("top", "right", "bottom", "left")
            if sides.get(side)
        )
    return f"border:{_px(width)} {style} {color}"


def _shadow_css(shadow: Any, *, inset: bool = False) -> str | None:
    """IRShadow → CSS box-shadow。

    blur>0 时按 N=10 环近似展开成多段（小 spread 在前 = 在上层），
    与手机 / 桌面同一套公式；blur=0 保持单段，输出与历史基准图一致。
    """
    if not isinstance(shadow, dict):
        return None
    layers = shadow_layers(shadow)
    if not layers:
        return None
    color = shadow.get("color")

    def length(value: float) -> str:
        # CSS 允许 0 不带单位；统一成 0 让输出更紧凑，也和历史基准图一致。
        return "0" if abs(value) < 0.0001 else _px(value)

    parts: list[str] = []
    for layer in reversed(layers):
        chunk = ["inset"] if inset or layer.get("inset") is True else []
        chunk.append(length(_num(layer.get("dx"), 0.0)))
        chunk.append(length(_num(layer.get("dy"), 0.0)))
        # blur 恒为 0：模糊完全由多层 spread 的叠加模拟。
        chunk.append("0")
        spread = _num(layer.get("spread"), 0.0)
        if abs(spread) > 0.0001:
            chunk.append(_px(spread))
        chunk.append(_rgba_css(color, _num(layer.get("alpha"), 0.0)))
        parts.append(" ".join(chunk))
    return "box-shadow:" + ",".join(parts)


def _default_radius(ntype: str, w: float, h: float) -> float:
    """历史入口：默认圆角只由编译层定义。"""
    return default_radius(ntype, w, h)


def _default_fill(ntype: str) -> str:
    """历史入口：默认填充只由编译层定义。"""
    return ir_color_css(default_fill_color(ntype), "#000000")


def _background(raw: Any, fallback: str) -> str:
    """历史入口：填充字符串 → CSS（渐变带角度，失败用 fallback）。"""
    fill = compile_fill(raw, None)
    if fill.get("kind") == "solid":
        return ir_color_css(fill.get("color"), fallback)
    if fill.get("kind") in ("linear", "radial"):
        return _gradient_css(fill)
    return fallback


def _single_color(raw: Any, fallback: str) -> str:
    """等价于 designColor：支持 #rgb / #rgba / #rrggbb / #aarrggbb / rgb() / hsl() / 具名色。"""
    return color_css(raw, fallback)


def _parse_color(value: str) -> tuple[int, int, int, int] | None:
    return _parse_color_impl(value)


def _alpha_css(color: str, alpha: float) -> str:
    return alpha_css(color, alpha)


from .design_tokens import parse_color as _parse_color_impl  # noqa: E402  (循环友好)

__all__ = [
    "ACCENT",
    "INK",
    "INPUT_FILL",
    "SHELL_TOP",
    "SWITCH_OFF",
    "render_design_html",
    "render_ir_html",
    "rgba_hex",
    "css_color",
]

# ------------------------------------------------------------------ 模板

_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="robots" content="noindex,nofollow">
<meta name="theme-color" content="#0B0E12">
<title>{title}</title>
<style>{css}</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <div class="badge">演</div>
    <div class="meta">
      <div class="title">演示预览</div>
      <div class="sub">{name}</div>
    </div>
    {count}
  </header>
  {main}
</div>
<script>
(function () {{
  "use strict";
  var SCREENS = {screens_json};
  var devices = Array.prototype.slice.call(document.querySelectorAll(".device"));
  var holder = document.getElementById("holder");
  var stage = document.querySelector(".stage");
  var countEl = document.getElementById("count");
  var prevEl = document.getElementById("prev");
  var nextEl = document.getElementById("next");
  var dotsEl = document.getElementById("dots");
  var index = 0;
  var dragSlider = null;

  function norm(value) {{
    return String(value == null ? "" : value).toLowerCase().replace(/[^\\p{{L}}\\p{{N}}]/gu, "");
  }}

  // 与移动端 demoNextScreen 同一套规则：返回类回上一屏；文案命中其他画板名/尾段/id 跳那屏；
  // 其余顺延到下一屏，末屏不动。
  function nextScreen(text, current) {{
    var raw = String(text == null ? "" : text).trim();
    if ({back_words}.some(function (word) {{ return raw.indexOf(word) >= 0; }})) {{
      return current > 0 ? current - 1 : null;
    }}
    var value = norm(raw);
    if (value) {{
      for (var i = 0; i < SCREENS.length; i++) {{
        if (i === current) continue;
        var screen = SCREENS[i] || {{}};
        var name = String(screen.name || "");
        var whole = norm(name);
        var tail = norm(name.indexOf("·") >= 0 ? name.slice(name.indexOf("·") + 1) : name);
        var id = norm(screen.id);
        if ((tail && (tail.indexOf(value) >= 0 || value.indexOf(tail) >= 0)) ||
            id.indexOf(value) >= 0 || whole.indexOf(value) >= 0) {{
          return i;
        }}
      }}
    }}
    return current + 1 < SCREENS.length ? current + 1 : null;
  }}

  // autoFit：单行放不下时按 0.5px 步长缩字号，最低 60%，与手机/桌面的自适应规则一致。
  function fitTexts() {{
    Array.prototype.forEach.call(document.querySelectorAll("[data-fit]"), function (el) {{
      var base = parseFloat(el.getAttribute("data-fit")) || 13;
      var min = base * 0.6;
      var size = base;
      el.style.fontSize = size + "px";
      while (size > min && el.scrollWidth > el.clientWidth + 0.5) {{
        size -= 0.5;
        el.style.fontSize = size + "px";
      }}
    }});
  }}

  function fit() {{
    var device = devices[index];
    if (!device || !stage) return;
    var dw = parseFloat(device.getAttribute("data-w")) || 1;
    var dh = parseFloat(device.getAttribute("data-h")) || 1;
    var scale = Math.min(stage.clientWidth / dw, stage.clientHeight / dh) * 0.98;
    if (!isFinite(scale) || scale <= 0) scale = 1;
    device.style.transform = "scale(" + scale + ")";
    holder.style.width = (dw * scale) + "px";
    holder.style.height = (dh * scale) + "px";
  }}

  function show(target) {{
    if (!devices.length) return;
    if (target < 0 || target >= devices.length) return;
    index = target;
    devices.forEach(function (device, i) {{
      device.classList.toggle("active", i === target);
    }});
    if (countEl) countEl.textContent = (target + 1) + " / " + devices.length;
    if (dotsEl) {{
      Array.prototype.forEach.call(dotsEl.children, function (dot, i) {{
        dot.classList.toggle("on", i === target);
      }});
    }}
    if (prevEl) prevEl.disabled = target === 0;
    if (nextEl) nextEl.disabled = target === devices.length - 1;
    fit();
    fitTexts();
  }}

  function setSlider(el, clientX) {{
    var rect = el.getBoundingClientRect();
    var width = rect.width || 1;
    var value = Math.max(0, Math.min(1, (clientX - rect.left) / width));
    el.style.setProperty("--v", String(value));
  }}

  if (devices.length) {{
    devices.forEach(function (_, i) {{
      var dot = document.createElement("button");
      dot.type = "button";
      dot.className = "dot";
      dot.setAttribute("aria-label", "第 " + (i + 1) + " 屏");
      dot.addEventListener("click", function () {{ show(i); }});
      if (dotsEl) dotsEl.appendChild(dot);
    }});

    if (prevEl) prevEl.addEventListener("click", function () {{ show(index - 1); }});
    if (nextEl) nextEl.addEventListener("click", function () {{ show(index + 1); }});

    holder.addEventListener("pointerdown", function (event) {{
      var slider = event.target.closest ? event.target.closest('[data-type="slider"]') : null;
      if (!slider) return;
      event.preventDefault();
      dragSlider = slider;
      if (slider.setPointerCapture) {{
        try {{ slider.setPointerCapture(event.pointerId); }} catch (e) {{}}
      }}
      setSlider(slider, event.clientX);
    }});

    holder.addEventListener("pointermove", function (event) {{
      if (!dragSlider) return;
      event.preventDefault();
      setSlider(dragSlider, event.clientX);
    }});

    function endDrag() {{ dragSlider = null; }}
    holder.addEventListener("pointerup", endDrag);
    holder.addEventListener("pointercancel", endDrag);
    window.addEventListener("pointerup", endDrag);

    holder.addEventListener("click", function (event) {{
      var node = event.target.closest ? event.target.closest("[data-type]") : null;
      if (!node) return;
      var type = node.getAttribute("data-type");
      if (type === "button" || type === "pill") {{
        var label = node.getAttribute("data-text") || node.getAttribute("data-name");
        var target = nextScreen(label, index);
        if (target !== null) show(target);
      }} else if (type === "switch") {{
        node.classList.toggle("on");
      }} else if (type === "nav") {{
        var items;
        try {{ items = JSON.parse(node.getAttribute("data-items") || "[]"); }}
        catch (e) {{ items = []; }}
        if (!items.length) items = ["首页", "任务", "我的"];
        var rect = node.getBoundingClientRect();
        var cell = rect.width / items.length;
        var active = Math.round((event.clientX - rect.left) / (cell || 1));
        active = Math.max(0, Math.min(items.length - 1, active));
        Array.prototype.forEach.call(node.querySelectorAll(".nav-cell"), function (cellEl, i) {{
          cellEl.classList.toggle("on", i === active);
        }});
        var next = nextScreen(items[active], index);
        if (next !== null) show(next);
      }}
    }});

    holder.addEventListener("keydown", function (event) {{
      if (event.key !== "Enter" && event.key !== " ") return;
      var node = event.target.closest ? event.target.closest('[data-type="button"],[data-type="pill"]') : null;
      if (!node) return;
      event.preventDefault();
      node.click();
    }});

    document.addEventListener("keydown", function (event) {{
      if (event.key === "ArrowLeft") show(index - 1);
      if (event.key === "ArrowRight") show(index + 1);
    }});

    window.addEventListener("resize", fit);
    window.addEventListener("orientationchange", function () {{ setTimeout(fit, 120); }});
    if (window.ResizeObserver) new ResizeObserver(fit).observe(stage);
    show(0);
  }}
}})();
</script>
</body>
</html>
"""

_CSS = """
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;background:#0B0E12}
body{
  color:#fff;overflow:hidden;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  -webkit-font-smoothing:antialiased;
}
.wrap{position:fixed;inset:0;display:flex;flex-direction:column}
.top{display:flex;align-items:center;gap:10px;flex:none;padding:calc(10px + env(safe-area-inset-top)) 18px 4px}
.badge{width:26px;height:26px;border-radius:50%;background:rgba(255,255,255,.12);display:flex;align-items:center;justify-content:center;font-size:11px;color:#fff;flex:none}
.meta{flex:1;min-width:0}
.title{font-size:12px;font-weight:500;color:#fff}
.sub{font-size:9px;color:rgba(255,255,255,.45);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.count{flex:none;font-size:9px;color:rgba(255,255,255,.4);font-variant-numeric:tabular-nums}
.stage{flex:1;min-height:0;display:flex;align-items:center;justify-content:center;padding:2px 20px}
.holder{position:relative;flex:none}
.device{position:absolute;top:0;left:0;transform-origin:top left;border-radius:26px;overflow:hidden;display:none;background:#fff;box-shadow:0 26px 60px rgba(0,0,0,.55)}
.device.active{display:block}
.screen{position:absolute;inset:0}
.empty{font-size:12px;color:rgba(255,255,255,.45)}
.foot{flex:none;display:flex;flex-direction:column;align-items:center;gap:8px;padding:6px 18px calc(10px + env(safe-area-inset-bottom))}
.hint{font-size:8.5px;color:rgba(255,255,255,.28)}
.pager{display:flex;align-items:center;gap:4px;padding:4px 8px;border-radius:999px;background:rgba(255,255,255,.10)}
.pg{width:26px;height:26px;padding:0;border:0;border-radius:50%;background:rgba(255,255,255,.14);color:rgba(255,255,255,.82);font:inherit;font-size:15px;line-height:1;display:flex;align-items:center;justify-content:center;cursor:pointer}
.pg:disabled{background:rgba(255,255,255,.05);color:rgba(255,255,255,.22);cursor:default}
.dots{display:flex;align-items:center;gap:3px;padding:0 4px}
.dot{width:6px;height:6px;padding:0;border:0;border-radius:50%;background:rgba(255,255,255,.28);cursor:pointer;transition:width .15s,height .15s,background .15s}
.dot.on{width:8px;height:8px;background:#fff}

.n{position:absolute}
.t-text{display:flex;align-items:center;overflow:hidden}
.t-button,.t-pill{display:flex;align-items:center;overflow:hidden;cursor:pointer;-webkit-tap-highlight-color:transparent;transition:filter .12s ease}
.t-button:active,.t-pill:active{filter:brightness(.94)}
.t-card{overflow:visible}
.t-decor,.t-circle{pointer-events:none}
.t-nav{display:flex;overflow:hidden;cursor:pointer}
.t-input{display:flex;align-items:center;overflow:hidden}
.t-input input{width:100%;height:100%;border:0;outline:0;background:transparent;font:inherit;color:inherit;padding:0}
.t-input input::placeholder{color:currentColor;opacity:.5}
.t-slider{cursor:pointer;touch-action:none;-webkit-tap-highlight-color:transparent}
.t-progress{overflow:hidden;pointer-events:none}
.pg-track,.pg-fill{position:absolute;left:0;top:0;height:100%;border-radius:inherit}
.pg-track{width:100%}
.pg-fill{width:calc(var(--v, .5) * 100%)}
.t-badge,.t-chip,.t-avatar{display:flex;align-items:center;justify-content:center;overflow:hidden}
.t-switch{background:#E2E8F0;cursor:pointer;transition:background-color .18s ease;-webkit-tap-highlight-color:transparent}
.t-switch.on{background:var(--c-on,#0891B2)}
.sw-knob{position:absolute;top:var(--knob-top,3px);left:3px;width:var(--knob,28px);height:var(--knob,28px);border-radius:50%;background:#fff;box-shadow:0 1px 2px rgba(0,0,0,.18);transition:left .18s cubic-bezier(.2,.8,.2,1)}
.t-switch.on .sw-knob{left:calc(100% - var(--knob,28px) - 3px)}

.tx{display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:3;overflow:hidden;width:100%;white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word}
.tx-fill{-webkit-background-clip:text;background-clip:text;color:transparent !important}
.tx-autofit{white-space:nowrap;text-overflow:ellipsis}

.sl-track,.sl-fill{position:absolute;left:0;top:50%;height:4px;margin-top:-2px;border-radius:2px}
.sl-track{width:100%}
.sl-fill{width:calc(var(--v, .5) * 100%)}
.sl-thumb,.sl-ring{position:absolute;top:50%;left:calc(var(--v, .5) * 100%);border-radius:50%}
.sl-thumb{width:16px;height:16px;margin-top:-8px;margin-left:-8px;background:#fff}
.sl-ring{width:20px;height:20px;margin-top:-10px;margin-left:-10px;pointer-events:none}

.nav-cell{flex:1;min-width:0;position:relative;display:flex;align-items:center;justify-content:center}
.nav-cell span{transform:translateY(-2px);max-width:100%;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.nav-cell i{position:absolute;bottom:4px;left:50%;width:12px;height:2px;margin-left:-6px;border-radius:1px;opacity:0}
.nav-cell.on i{opacity:.9}
"""
