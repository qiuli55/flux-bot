"""设计稿渲染契约测试。

`doc/design-spec.json` 是三端（手机端 DesignSpec.kt / 后端 design_render.py / 桌面端
DesignCanvas.cs）共用的唯一事实来源。这里读取该文件，逐条断言后端渲染器确实按契约
输出，避免某一端悄悄改默认值导致"网页预览"和 App 内演示对不上。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from flux.core import design_render as render

SPEC_PATH = Path(__file__).resolve().parents[2] / "doc" / "design-spec.json"


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    return json.loads(SPEC_PATH.read_text(encoding="utf-8"))


def _text(node: dict[str, Any]) -> str:
    return render._node_html({"w": 100, "h": 40, **node})


# ------------------------------------------------------------------ 颜色常量


def test_color_constants_match_spec(spec: dict[str, Any]) -> None:
    colors = spec["colors"]
    assert render.ACCENT.lower() == colors["accent"].lower()
    assert render.INK.lower() == colors["ink"].lower()
    assert render.SHELL_TOP.lower() == colors["shellTop"].lower()
    assert render.SWITCH_OFF.lower() == colors["switchOff"].lower()
    assert render.INPUT_FILL.lower() == colors["inputFill"].lower()


# ------------------------------------------------------------------ 排版


def test_text_defaults_match_spec(spec: dict[str, Any]) -> None:
    typo = spec["typography"]
    css = render._text_css({})
    assert f"font-size:{typo['defaultFontSize']}px" in css
    assert f"line-height:{typo['defaultLineHeight']}" in css
    assert f"letter-spacing:{typo['defaultLetterSpacing']}px" in css
    assert "font-weight:400" in css
    # 十六进制统一小写输出，比较时忽略大小写（契约值本身是 #0F172A 这种写法）
    assert f"color:{render.INK}".lower() in css.lower()


def test_text_clamped_to_spec_lines(spec: dict[str, Any]) -> None:
    assert f"-webkit-line-clamp:{spec['typography']['maxTextLines']}" in render._CSS


def test_font_stacks_match_spec(spec: dict[str, Any]) -> None:
    """网页壳里的字体栈必须与 `design-spec.json` 的 fonts.stacks 完全一致。"""
    fonts = spec["fonts"]
    assert set(fonts["stacks"]) == set(render._FONT_STACKS)
    for family, expected in fonts["stacks"].items():
        tokens = [
            token.strip().strip('"').strip("'") for token in render._FONT_STACKS[family].split(",")
        ]
        assert tokens == expected, family


def test_line_break_contract_matches_css(spec: dict[str, Any]) -> None:
    rules = spec["lineBreaking"]
    assert "word-break:break-word" in render._CSS
    assert rules["clamp"].startswith("maxLines 缺省 3")
    assert spec["typography"]["textLinesLimit"] == 64
    assert spec["typography"]["autoFitMinScale"] == 0.6
    assert spec["typography"]["autoFitStep"] == 0.5


def test_nav_font_size_matches_spec(spec: dict[str, Any]) -> None:
    html = render._node_html(
        {"type": "nav", "w": 300, "h": 60, "items": ["首页", "任务", "我的"], "active": 1}
    )
    size = spec["typography"]["navFontSize"]
    assert f"font-size:{size}px" in html
    assert f"font-weight:{spec['typography']['navActiveWeight']}" in html
    assert f"font-weight:{spec['typography']['navIdleWeight']}" in html


def test_nav_defaults_match_spec(spec: dict[str, Any]) -> None:
    nav = spec["nav"]
    html = render._node_html({"type": "nav", "w": 300, "h": 60})
    for item in nav["defaultItems"]:
        assert item in html
    # 默认选中第一格
    assert 'class="nav-cell on"' in html
    # 未选中文字透明度
    assert render._alpha_css(render.INK, nav["idleAlpha"]) in html
    # 行高与移动端 `size * 1.2` 一致
    assert f"line-height:{nav['lineHeight']}" in html


def test_screen_bg_uses_document_bg(spec: dict[str, Any]) -> None:
    assert render.SHELL_TOP.lower() == spec["screen"]["bgDefault"].lower()
    # screen.bg 被忽略，画板底色只认文档级 bg
    html = render.render_design_html(
        {
            "bg": "#101010",
            "screens": [{"w": 300, "h": 600, "bg": "#FF0000", "nodes": []}],
        }
    )
    assert "background:#101010" in html
    assert "#ff0000" not in html.lower()
    default_html = render.render_design_html({"screens": [{"w": 300, "h": 600, "nodes": []}]})
    assert f"background:{render.SHELL_TOP}".lower() in default_html.lower()


def test_input_placeholder_alpha_matches_spec(spec: dict[str, Any]) -> None:
    alpha = spec["inputPlaceholderAlpha"]
    assert alpha == 0.5
    assert "opacity:.5" in render._CSS


# ------------------------------------------------------------------ 几何


def test_geometry_matches_spec(spec: dict[str, Any]) -> None:
    geo = spec["geometry"]
    css = render._CSS

    def css_alpha(value: float) -> str:
        # CSS 省略小数前导 0 与末尾 0：0.55 -> .55，0.9 -> .9
        text = f"{value:.2f}".rstrip("0").rstrip(".")
        return text.replace("0.", ".")

    # 设备外框
    assert f"border-radius:{geo['deviceRadius']}px" in css
    shadow = geo["deviceShadow"]
    assert (
        f"box-shadow:0 {shadow['dy']}px {shadow['blur']}px rgba(0,0,0,"
        f"{css_alpha(shadow['alpha'] / 255)})" in css
    )

    # 卡片投影（纯色固定投影）
    card_shadow = geo["cardShadow"]
    card = render._node_html({"type": "card", "w": 100, "h": 60})
    assert f"box-shadow:0 {card_shadow['dy']}px" in card
    # 计算出的颜色统一三位小数（与 _alpha_css / 默认填充同一格式）
    assert f"rgba(0,0,0,{card_shadow['alpha']:.3f})" in card

    # 导航指示条
    nav_ind = geo["navIndicator"]
    assert f"width:{nav_ind['width']}px" in css
    assert f"height:{nav_ind['height']}px" in css
    assert f"bottom:{nav_ind['bottom']}px" in css
    assert f"opacity:{css_alpha(nav_ind['alpha'])}" in css

    # 导航文字上移
    assert f"translateY({geo['navLabelOffsetY']}px)" in css

    # 开关 / 滑杆
    switch = render._node_html({"type": "switch", "w": 50, "h": 30})
    assert f"--knob:{30 - 2 * geo['switchKnobInset']}px" in switch
    slider = render._node_html({"type": "slider", "w": 200, "h": 30, "value": 0.5})
    assert f"height:{geo['sliderTrackHeight']}px" in css
    assert f"width:{geo['sliderThumb']}px" in slider or f"width:{geo['sliderThumb']}px" in css
    assert f"width:{geo['sliderRing']}px" in slider or f"width:{geo['sliderRing']}px" in css
    assert f"{geo['sliderRingAlpha']:.3f}" in slider

    # 输入框左内边距
    pad = geo["inputPad"]
    inp = render._node_html({"type": "input", "w": 200, "h": 40, "text": "说点什么"})
    assert f"padding-left:{pad}px" in inp
    inp_custom = render._node_html(
        {"type": "input", "w": 200, "h": 40, "padLeft": 20, "text": "说点什么"}
    )
    assert "padding-left:20px" in inp_custom


# ------------------------------------------------------------------ 默认圆角 / 填充


@pytest.mark.parametrize("ntype", ["button", "pill", "input", "slider", "switch"])
def test_default_radius_half_height(spec: dict[str, Any], ntype: str) -> None:
    assert spec["defaults"]["radius"][ntype] == "h/2"
    assert render._default_radius(ntype, 100.0, 40.0) == 20.0


@pytest.mark.parametrize("ntype", ["card", "nav"])
def test_default_radius_card_nav(spec: dict[str, Any], ntype: str) -> None:
    radius = spec["defaults"]["radius"][ntype]
    assert render._default_radius(ntype, 100.0, 40.0) == radius


def test_default_radius_circle_and_text(spec: dict[str, Any]) -> None:
    assert spec["defaults"]["radius"]["circle"] == "w/2"
    assert render._default_radius("circle", 60.0, 40.0) == 30.0
    assert render._default_radius("text", 60.0, 40.0) == spec["defaults"]["radius"]["text"]


def test_explicit_radius_wins() -> None:
    html = render._node_html({"type": "card", "w": 100, "h": 60, "radius": 4})
    assert "border-radius:4px" in html


def test_default_fill_matches_spec(spec: dict[str, Any]) -> None:
    fill = spec["defaults"]["fill"]
    assert fill["pill"] == "accent@0.12"
    assert fill["decor"] == "accent@0.12"
    assert fill["other"] == "ink@0.08"
    # IR 颜色统一小写十六进制，契约里的写法大小写不敏感
    assert render._default_fill("button").lower() == render.ACCENT.lower()
    assert render._default_fill("pill") == "rgba(8,145,178,0.120)"
    assert render._default_fill("card").lower() == fill["card"].lower()
    assert render._default_fill("nav").lower() == fill["nav"].lower()
    assert render._default_fill("input").lower() == render.INPUT_FILL.lower()
    assert render._default_fill("decor") == "rgba(8,145,178,0.120)"
    assert render._default_fill("switch").lower() == render.ACCENT.lower()
    assert render._default_fill("slider").lower() == render.ACCENT.lower()
    assert render._default_fill("whatever") == "rgba(15,23,42,0.080)"


# ------------------------------------------------------------------ 文字对齐


@pytest.mark.parametrize("ntype", ["button", "pill"])
def test_button_and_pill_force_center(spec: dict[str, Any], ntype: str) -> None:
    assert spec["defaults"]["textAlign"][ntype] == "center"
    for align in ("", "left", "right", "center"):
        html = _text({"type": ntype, "text": "开始使用", "align": align})
        assert "text-align:center" in html
        assert "text-align:left" not in html
        assert "text-align:right" not in html


@pytest.mark.parametrize("ntype", ["text", "input"])
def test_other_types_follow_align(spec: dict[str, Any], ntype: str) -> None:
    assert spec["defaults"]["textAlign"][ntype] == "align or left"
    assert "text-align:right" in _text({"type": ntype, "text": "标题", "align": "right"})
    assert "text-align:left" in _text({"type": ntype, "text": "标题", "align": "bogus"})


# ------------------------------------------------------------------ 颜色解析


def test_color_parsing_matches_spec(spec: dict[str, Any]) -> None:
    assert spec["colorParsing"]["hex3"] == "expand to hex6"
    assert render._single_color("#abc", "") == "#aabbcc"
    assert render._single_color("#0F172A", "") == "#0f172a"
    # #aarrggbb 按 Android ARGB 解析：80 是 alpha
    assert render._single_color("#80FF0000", "") == "rgba(255,0,0,0.502)"
    assert render._single_color("rgb(255, 0, 0)", "") == "#ff0000"
    assert render._single_color("rgba(8,145,178,0.12)", "") == "rgba(8,145,178,0.122)"
    assert render._single_color("", "#123456") == "#123456"
    assert render._single_color("not-a-color", "#123456") == "#123456"


def test_gradient_single_color_uses_last_stop(spec: dict[str, Any]) -> None:
    assert "single-color fallback uses the last stop" in spec["colorParsing"]["gradient"]
    assert render._single_color("linear-gradient(#000000, #FFFFFF)", "#123456") == "#ffffff"
    bg = render._background("linear-gradient(#000000, #FFFFFF)", "#123456")
    assert bg == "linear-gradient(180deg, #000000, #ffffff)"


# ------------------------------------------------------------------ 跳过规则


def test_missing_type_defaults_to_text(spec: dict[str, Any]) -> None:
    assert spec["skipNode"]["typeMissing"] == "text"
    assert spec["skipNode"]["when"][0] == "hidden is true"
    html = render._node_html({"w": 100, "h": 40, "text": "无类型"})
    assert 'data-type="text"' in html
    assert "t-text" in html


def test_zero_size_and_transparent_nodes_are_skipped(spec: dict[str, Any]) -> None:
    assert spec["skipNode"]["when"][1] == "w <= 0 or h <= 0"
    assert spec["skipNode"]["when"][2] == "opacity <= 0.01"
    assert render._node_html({"type": "card", "w": 0, "h": 40}) == ""
    assert render._node_html({"type": "card", "w": 100, "h": -1}) == ""
    assert render._node_html({"type": "card", "w": 100, "h": 40, "opacity": 0.01}) == ""
    assert render._node_html({"type": "card", "w": 100, "h": 40, "opacity": 0.5}) != ""


# ------------------------------------------------------------------ 溢出裁剪


def test_overflow_contract(spec: dict[str, Any]) -> None:
    clipped = spec["overflow"]["clip"]
    assert set(clipped) == {"text", "button", "pill", "input", "nav", "badge", "chip", "avatar"}
    css = render._CSS
    assert ".t-text{display:flex;align-items:center;overflow:hidden}" in css
    assert ".t-button,.t-pill{display:flex;align-items:center;overflow:hidden" in css
    assert ".t-input{display:flex;align-items:center;overflow:hidden}" in css
    assert ".t-nav{display:flex;overflow:hidden" in css
    badge_selector = (
        ".t-badge,.t-chip,.t-avatar{display:flex;align-items:center;"
        "justify-content:center;overflow:hidden}"
    )
    assert badge_selector in css
    visible = spec["overflow"]["visible"]
    assert visible == ["card"]
    assert ".t-card{overflow:visible}" in css


# ------------------------------------------------------------------ 旋转


def test_rotation_becomes_css_transform(spec: dict[str, Any]) -> None:
    assert spec["transform"]["rotationField"] == "rotation"
    assert spec["transform"]["unit"] == "degrees clockwise"
    assert spec["transform"]["origin"] == "center"
    html = render._node_html({"type": "card", "w": 100, "h": 60, "rotation": 30})
    assert "transform:rotate(30deg)" in html
    # transform-origin 默认 50% 50%，无需显式声明
    assert "transform-origin" not in html
    assert "transform:" not in render._node_html(
        {"type": "card", "w": 100, "h": 60, "rotation": 0.005}
    )
