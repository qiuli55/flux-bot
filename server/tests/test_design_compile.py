"""编译层（JSON → IR）契约测试。

`doc/design-ir.json` 是三端消费的 IR 契约；`doc/design-spec.json` 是默认值契约。
这里逐条断言 `design_compile` 把隐式默认全部展开成显式值，避免渲染端再各写一套推断。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from flux.core import design_compile as comp

ROOT = Path(__file__).resolve().parents[2]
IR_PATH = ROOT / "doc" / "design-ir.json"
CASES_DIR = ROOT / "doc" / "design-cases"

REQUIRED_NODE_KEYS = {
    "id",
    "type",
    "name",
    "parentType",
    "x",
    "y",
    "w",
    "h",
    "rotation",
    "opacity",
    "radius",
    "fill",
    "stroke",
    "shadow",
    "insetShadow",
    "blur",
    "clip",
    "ellipse",
    "text",
    "children",
}


@pytest.fixture(scope="module")
def ir_doc() -> dict[str, Any]:
    return json.loads(IR_PATH.read_text(encoding="utf-8"))


def node(**kwargs: Any) -> dict[str, Any]:
    base = {"type": "card", "x": 0, "y": 0, "w": 100, "h": 40}
    base.update(kwargs)
    return base


def compile_one(**kwargs: Any) -> dict[str, Any]:
    compiled = comp.compile_node(node(**kwargs))
    assert compiled is not None
    return compiled


# ------------------------------------------------------------------ 文档 / 画板


def test_document_defaults() -> None:
    ir = comp.compile_design({})
    assert ir["version"] == comp.IR_VERSION
    assert ir["name"] == "未命名设计稿"
    assert ir["bg"] == {"hex": "#f4f6f9"}
    assert ir["screens"] == []


def test_document_bg_wins_over_screen_bg() -> None:
    ir = comp.compile_design(
        {
            "bg": "#101010",
            "screens": [{"w": 300, "h": 600, "bg": "#FF0000", "nodes": []}],
        }
    )
    assert ir["bg"] == {"hex": "#101010"}
    assert ir["screens"][0]["bg"] == {"hex": "#101010"}


def test_invalid_screens_are_dropped() -> None:
    ir = comp.compile_design(
        {"screens": [{"w": 0, "h": 600}, {"w": 300, "h": -1}, "bad", {"w": 300, "h": 600}]}
    )
    assert len(ir["screens"]) == 1


# ------------------------------------------------------------------ 类型 / 跳过规则


def test_type_is_trimmed_lowered_and_defaulted() -> None:
    assert comp.normalize_type("  BUTTON ") == "button"
    assert comp.normalize_type(None) == "text"
    assert compile_one(type=" CARD ")["type"] == "card"


def test_hidden_flag_variants() -> None:
    assert comp.flag("false") is False
    assert comp.flag("0") is False
    assert comp.flag("") is False
    assert comp.flag("none") is False
    assert comp.flag("true") is True
    assert comp.flag(True) is True
    assert comp.compile_node(node(hidden="false")) is not None
    assert comp.compile_node(node(hidden="true")) is None
    assert comp.compile_node(node(hidden=True)) is None


def test_skip_rules() -> None:
    assert comp.compile_node(node(w=0)) is None
    assert comp.compile_node(node(h=0)) is None
    assert comp.compile_node(node(h=-1)) is None
    assert comp.compile_node(node(opacity=0.01)) is None
    assert comp.compile_node(node(opacity=0.02)) is not None
    assert comp.compile_node("not-a-node") is None


# ------------------------------------------------------------------ 圆角


@pytest.mark.parametrize("ntype", ["button", "pill", "input", "slider", "switch"])
def test_default_radius_half_height(ntype: str) -> None:
    assert comp.radius_of(ntype, 100.0, 40.0, None) == {
        "tl": 20.0,
        "tr": 20.0,
        "br": 20.0,
        "bl": 20.0,
    }


@pytest.mark.parametrize("ntype", ["card", "nav"])
def test_default_radius_card_nav(ntype: str) -> None:
    assert comp.radius_of(ntype, 100.0, 40.0, None)["tl"] == 18.0


def test_circle_radius_uses_width_and_clamps() -> None:
    assert comp.radius_of("circle", 60.0, 60.0, None)["tl"] == 30.0
    # 上限 min(w, h) / 2：宽圆角不会被压成椭圆
    assert comp.radius_of("circle", 100.0, 40.0, None)["tl"] == 20.0


def test_explicit_radius_forms() -> None:
    assert comp.radius_of("card", 100.0, 60.0, 4) == {"tl": 4.0, "tr": 4.0, "br": 4.0, "bl": 4.0}
    assert comp.radius_of("card", 100.0, 60.0, [1, 2, 3, 4]) == {
        "tl": 1.0,
        "tr": 2.0,
        "br": 3.0,
        "bl": 4.0,
    }
    assert comp.radius_of("card", 100.0, 60.0, {"topLeft": 6, "br": 2}) == {
        "tl": 6.0,
        "tr": 18.0,
        "br": 2.0,
        "bl": 18.0,
    }
    assert comp.radius_of("card", 100.0, 60.0, {"topRight": 7})["tr"] == 7.0


def test_negative_radius_clamps_to_zero_not_default() -> None:
    assert comp.radius_of("card", 100.0, 60.0, -5)["tl"] == 0.0
    assert comp.radius_of("card", 100.0, 60.0, 999)["tl"] == 30.0


# ------------------------------------------------------------------ 颜色 / 填充


def test_color_syntaxes() -> None:
    assert comp.ir_color("#f80") == {"hex": "#ff8800"}
    assert comp.ir_color("#0891B2") == {"hex": "#0891b2"}
    assert comp.ir_color("#800891B2") == {"hex": "#0891b2", "alpha": 0.501961}
    assert comp.ir_color("rgb(8,145,178)") == {"hex": "#0891b2"}
    assert comp.ir_color("rgba(8,145,178,0.5)") == {"hex": "#0891b2", "alpha": 0.501961}
    assert comp.ir_color("rgb(100%,0%,0%)") == {"hex": "#ff0000"}
    assert comp.ir_color("hsl(0,100%,50%)") == {"hex": "#ff0000"}
    assert comp.ir_color("tomato") == {"hex": "#ff6347"}
    assert comp.ir_color("not-a-color") is None


def test_fill_defaults_per_type() -> None:
    assert compile_one(type="button")["fill"] == {"kind": "solid", "color": {"hex": "#0891b2"}}
    assert compile_one(type="pill")["fill"] == {
        "kind": "solid",
        "color": {"hex": "#0891b2", "alpha": 0.12},
    }
    assert compile_one(type="card")["fill"] == {"kind": "solid", "color": {"hex": "#ffffff"}}
    assert compile_one(type="input")["fill"] == {"kind": "solid", "color": {"hex": "#f8fafc"}}
    assert compile_one(type="decor")["fill"] == {
        "kind": "solid",
        "color": {"hex": "#0891b2", "alpha": 0.12},
    }
    assert compile_one(type="whatever")["fill"] == {
        "kind": "solid",
        "color": {"hex": "#0f172a", "alpha": 0.08},
    }


def test_text_without_fill_has_no_background() -> None:
    assert compile_one(type="text", text="标题")["fill"] == {"kind": "none"}
    assert compile_one(type="text", text="标题", fill="none")["fill"] == {"kind": "none"}
    assert compile_one(type="text", text="标题", fill="transparent")["fill"] == {"kind": "none"}


def test_invalid_fill_falls_back_to_type_default() -> None:
    assert compile_one(type="card", fill="not-a-color")["fill"] == {
        "kind": "solid",
        "color": {"hex": "#ffffff"},
    }


def test_gradient_defaults_to_180deg() -> None:
    fill = comp.parse_gradient("linear-gradient(#000000, #FFFFFF)")
    assert fill is not None
    assert fill["kind"] == "linear"
    assert fill["angle"] == 180.0
    assert [stop["offset"] for stop in fill["stops"]] == [0.0, 1.0]
    assert fill["stops"][1]["color"] == {"hex": "#ffffff"}


@pytest.mark.parametrize(
    ("raw", "angle"),
    [
        ("linear-gradient(90deg,#000,#fff)", 90.0),
        ("linear-gradient(0.25turn,#000,#fff)", 90.0),
        ("linear-gradient(to right,#000,#fff)", 90.0),
        ("linear-gradient(to bottom right,#000,#fff)", 135.0),
        ("linear-gradient(to top left,#000,#fff)", 315.0),
        ("linear-gradient(-90deg,#000,#fff)", 270.0),
    ],
)
def test_gradient_angles(raw: str, angle: float) -> None:
    fill = comp.parse_gradient(raw)
    assert fill is not None
    assert fill["angle"] == angle


def test_gradient_stop_distribution() -> None:
    fill = comp.parse_gradient("linear-gradient(#000000, #ffffff 30%, #ff0000)")
    assert fill is not None
    assert [stop["offset"] for stop in fill["stops"]] == [0.0, 0.3, 1.0]


def test_radial_gradient_center() -> None:
    fill = comp.parse_gradient("radial-gradient(circle at 30% 70%, #ffffff, #000000)")
    assert fill is not None
    assert fill["kind"] == "radial"
    assert fill["center"] == {"x": 0.3, "y": 0.7}
    default = comp.parse_gradient("radial-gradient(#ffffff, #000000)")
    assert default is not None
    assert default["center"] == {"x": 0.5, "y": 0.5}


def test_conic_gradient_is_unsupported() -> None:
    assert comp.parse_gradient("conic-gradient(#000,#fff)") is None
    assert comp.compile_fill("conic-gradient(#000,#fff)", None) == {"kind": "none"}


def test_stroke_compilation() -> None:
    assert comp.compile_stroke("#0891B2") == {
        "color": {"hex": "#0891b2"},
        "width": 1.0,
        "align": "inside",
        "dash": [],
        "sides": None,
    }
    stroke = comp.compile_stroke(
        {"color": "#000", "width": 2, "align": "outside", "dash": [4, 2], "sides": ["top", "left"]}
    )
    assert stroke == {
        "color": {"hex": "#000000"},
        "width": 2.0,
        "align": "outside",
        "dash": [4.0, 2.0],
        "sides": {"top": True, "right": False, "bottom": False, "left": True},
    }
    assert comp.compile_stroke({"width": 2}) is None


def test_shadow_compilation() -> None:
    assert comp.compile_shadow({"dy": 6}) == {
        "dx": 0.0,
        "dy": 6.0,
        "blur": 0.0,
        "color": {"hex": "#000000", "alpha": 0.25},
        "inset": False,
    }
    assert comp.compile_shadow({"color": "#ff0000", "blur": -3, "inset": "true"}) == {
        "dx": 0.0,
        "dy": 0.0,
        "blur": 0.0,
        "color": {"hex": "#ff0000"},
        "inset": True,
    }
    assert comp.compile_shadow(None) is None


def test_card_gets_default_shadow() -> None:
    shadow = compile_one(type="card")["shadow"]
    assert shadow == {
        "dx": 0.0,
        "dy": 6.0,
        "blur": 0.0,
        "color": {"hex": "#000000", "alpha": 0.05},
        "inset": False,
    }


# ------------------------------------------------------------------ 文字


def test_text_defaults() -> None:
    style = comp.text_style({"text": "你好"}, "text")
    assert style["value"] == "你好"
    assert style["fontFamily"] == "system"
    assert style["fontSize"] == 13.0
    assert style["fontWeight"] == 400
    assert style["lineHeight"] == 1.35
    assert style["letterSpacing"] == 0.0
    assert style["align"] == "left"
    assert style["valign"] == "center"
    assert style["maxLines"] == 3
    assert style["ellipsis"] is True
    assert style["autoFit"] is False
    assert style["color"] == {"hex": "#0f172a"}
    assert style["padLeft"] == 0.0
    assert style["placeholderAlpha"] == 1.0


def test_blank_text_has_no_text_block() -> None:
    assert comp.compile_text({"text": "   "}, "text", 100, 40) is None
    assert comp.compile_text({}, "text", 100, 40) is None
    assert compile_one(type="text")["text"] is None


@pytest.mark.parametrize("ntype", ["button", "pill", "nav"])
def test_center_forced_types(ntype: str) -> None:
    assert comp.text_style({"text": "x", "align": "right"}, ntype)["align"] == "center"


def test_align_whitelist() -> None:
    assert comp.text_style({"align": "right"}, "text")["align"] == "right"
    assert comp.text_style({"align": "bogus"}, "text")["align"] == "left"


def test_input_placeholder_alpha_not_multiplied() -> None:
    style = comp.text_style({"text": "说点什么"}, "input")
    assert style["placeholderAlpha"] == 0.5
    # color 保持全不透明，透明度只由 placeholderAlpha 表达
    assert "alpha" not in style["color"]
    assert style["padLeft"] == 12.0
    assert comp.text_style({"padLeft": 20}, "input")["padLeft"] == 20.0


def test_font_family_aliases() -> None:
    assert comp.font_family("Serif") == "noto-serif-sc"
    assert comp.font_family("Noto Serif SC") == "noto-serif-sc"
    assert comp.font_family("noto_sans_sc") == "noto-sans-sc"
    assert comp.font_family("JetBrains Mono") == "mono"
    assert comp.font_family("monospace") == "mono"
    assert comp.font_family("Comic Sans") == "system"


def test_font_aliases_match_spec() -> None:
    """`doc/design-spec.json` 的 fonts.aliases 必须与编译层逐条一致（三端共用同一张表）。"""
    spec = json.loads((ROOT / "doc" / "design-spec.json").read_text(encoding="utf-8"))
    for alias, expected in spec["fonts"]["aliases"].items():
        assert comp.font_family(alias) == expected, alias
    assert set(spec["fonts"]["enums"]) == {
        "system",
        "noto-sans-sc",
        "noto-serif-sc",
        "inter",
        "mono",
    }
    assert spec["typography"]["defaultFontFamily"] == "system"


def test_text_effects() -> None:
    style = comp.text_style(
        {
            "text": "特效",
            "textStroke": {"color": "#fff", "width": 2},
            "textGlow": {"color": "#22D3EE", "blur": 6},
            "textShadow": {"color": "#000", "dx": 1, "dy": 2, "blur": 4, "intensity": 0.5},
        },
        "text",
    )
    assert style["stroke"] == {"width": 2.0, "color": {"hex": "#ffffff"}, "align": "outside"}
    assert style["glow"]["rings"] == [[0.35, 0.55], [0.7, 0.35], [1.0, 0.18]]
    assert style["glow"]["color"] == {"hex": "#22d3ee"}
    assert style["shadow"]["dx"] == 1.0
    assert style["shadow"]["intensity"] == 0.5


def test_auto_fit_and_max_lines_clamp() -> None:
    assert comp.text_style({"autoFit": "true"}, "text")["autoFit"] is True
    assert comp.text_style({"maxLines": 99}, "text")["maxLines"] == 64
    assert comp.text_style({"maxLines": 0}, "text")["maxLines"] == 1


def test_text_area_is_recorded() -> None:
    style = comp.compile_text({"text": "x"}, "text", 120, 24)
    assert style is not None
    assert style["area"] == {"w": 120.0, "h": 24.0}


# ------------------------------------------------------------------ 控件


def test_switch_control() -> None:
    control = compile_one(type="switch", w=50, h=30)["control"]
    assert control == {
        "on": False,
        "knob": 24.0,
        "inset": 3.0,
        "knobTop": 3.0,
        "onColor": {"hex": "#0891b2"},
        "offColor": {"hex": "#e2e8f0"},
    }
    assert compile_one(type="switch", w=50, h=30, on="true")["control"]["on"] is True


def test_slider_control() -> None:
    control = compile_one(type="slider", w=200, h=30, value=0.5)["control"]
    assert control["value"] == 0.5
    assert control["thumbX"] == 100.0
    assert control["trackHeight"] == 4.0
    assert control["thumb"] == 16.0
    assert control["ring"] == 20.0
    assert control["ringColor"] == {"hex": "#0891b2", "alpha": 0.22}
    assert compile_one(type="slider", w=200, h=30, value=2)["control"]["value"] == 1.0
    assert compile_one(type="slider", w=200, h=30, value=-1)["control"]["value"] == 0.0


def test_nav_control_defaults() -> None:
    control = compile_one(type="nav", w=300, h=60)["control"]
    assert [item["label"] for item in control["items"]] == ["首页", "任务", "我的"]
    assert [item["active"] for item in control["items"]] == [True, False, False]
    assert control["cellWidth"] == 100.0
    assert control["items"][1]["color"] == {"hex": "#0f172a", "alpha": 0.42}
    assert control["items"][0]["fontWeight"] == 600
    assert control["indicator"] == {"w": 12.0, "h": 2.0, "bottom": 4.0, "alpha": 0.9}
    assert control["labelOffsetY"] == -2.0


def test_nav_active_clamped() -> None:
    control = compile_one(type="nav", w=300, h=60, items=["A", "B"], active=9)["control"]
    assert [item["active"] for item in control["items"]] == [False, True]


# ------------------------------------------------------------------ 嵌套 / 裁剪


def test_children_are_compiled_with_parent_type() -> None:
    ir = compile_one(
        type="card",
        children=[
            {"type": "text", "w": 50, "h": 20, "text": "hi"},
            {"type": "text", "w": 0, "h": 20, "text": "gone"},
        ],
    )
    assert len(ir["children"]) == 1
    assert ir["children"][0]["parentType"] == "card"


@pytest.mark.parametrize("ntype", list(comp.CLIP_TYPES))
def test_clip_types(ntype: str) -> None:
    assert compile_one(type=ntype)["clip"] is True


def test_non_clip_types() -> None:
    assert compile_one(type="card")["clip"] is False
    assert compile_one(type="circle")["clip"] is False


def test_ellipse_detection() -> None:
    assert compile_one(type="circle", w=40, h=40)["ellipse"] is True
    assert compile_one(type="decor", w=40, h=40)["ellipse"] is True
    assert compile_one(type="decor", w=40, h=20)["ellipse"] is False
    assert compile_one(type="avatar", w=40, h=40)["ellipse"] is True


# ------------------------------------------------------------------ 新增类型


def test_new_type_defaults() -> None:
    # divider：一条线，无圆角
    divider = compile_one(type="divider", w=200, h=1)
    assert divider["radius"] == {"tl": 0.0, "tr": 0.0, "br": 0.0, "bl": 0.0}
    assert divider["fill"] == {"kind": "solid", "color": {"hex": "#0f172a", "alpha": 0.08}}

    # progress：h/2 胶囊，fill=accent，control 里带轨道与填充色
    progress = compile_one(type="progress", w=200, h=8, value=0.25)
    assert progress["radius"] == {"tl": 4.0, "tr": 4.0, "br": 4.0, "bl": 4.0}
    assert progress["control"]["value"] == 0.25
    assert progress["control"]["fillColor"] == {"hex": "#0891b2"}
    assert progress["control"]["trackColor"] == {"hex": "#e2e8f0"}

    # badge / chip / avatar：圆角与默认文字色
    assert compile_one(type="badge", w=38, h=20, text="3")["radius"]["tl"] == 10.0
    assert compile_one(type="chip", w=76, h=26, text="标签")["radius"]["tl"] == 13.0
    avatar = compile_one(type="avatar", w=44, h=44, text="L")
    assert avatar["radius"]["tl"] == 22.0
    assert avatar["ellipse"] is True


def test_new_type_text_defaults() -> None:
    assert comp.compile_text({"text": "3"}, "badge", 38, 20)["color"] == {"hex": "#ffffff"}
    assert comp.compile_text({"text": "标签"}, "chip", 76, 26)["color"] == {"hex": "#0891b2"}
    assert comp.compile_text({"text": "L"}, "avatar", 44, 44)["color"] == {"hex": "#0891b2"}
    assert comp.compile_text({"text": "x"}, "card", 40, 20)["color"] == {"hex": "#0f172a"}
    # 三端强制居中
    assert comp.compile_text({"text": "x", "align": "left"}, "badge", 38, 20)["align"] == "center"


def test_progress_value_clamped() -> None:
    assert compile_one(type="progress", value=2)["control"]["value"] == 1.0
    assert compile_one(type="progress", value=-1)["control"]["value"] == 0.0
    assert compile_one(type="progress")["control"]["value"] == 0.5


# ------------------------------------------------------------------ 阴影环


def test_shadow_layers_degenerate_to_single() -> None:
    shadow = comp.compile_shadow({"dy": 6, "color": "#000000"})
    assert shadow is not None
    layers = comp.shadow_layers(shadow)
    assert len(layers) == 1
    assert layers[0]["spread"] == 0.0
    assert layers[0]["alpha"] == 1.0


def test_shadow_layers_ring_approximation() -> None:
    shadow = comp.compile_shadow({"dy": 4, "blur": 10, "color": "rgba(0,0,0,0.5)"})
    assert shadow is not None
    layers = comp.shadow_layers(shadow)
    assert len(layers) == comp.SHADOW_RINGS
    # 大 spread 在前（在下层），小 spread 在后（在上层）
    assert [layer["spread"] for layer in layers] == sorted(
        (layer["spread"] for layer in layers), reverse=True
    )
    assert layers[0]["spread"] == 10.0
    assert layers[-1]["spread"] == 1.0
    assert layers[0]["alpha"] == 0.0
    assert layers[-1]["alpha"] > layers[0]["alpha"]
    # 总透明度≈原 alpha（环叠加近似）
    total = sum(layer["alpha"] for layer in layers)
    assert 0.4 < total <= 0.5


def test_shadow_layers_drop_invisible() -> None:
    shadow = comp.compile_shadow({"blur": 6, "color": "rgba(0,0,0,0)"})
    assert shadow is not None
    assert comp.shadow_layers(shadow) == []


# ------------------------------------------------------------------ 契约 / 用例


def test_ir_contract_matches_doc(ir_doc: dict[str, Any]) -> None:
    assert ir_doc["version"] == comp.IR_VERSION
    assert set(ir_doc["IRDocument"]) == set(comp.compile_design({}))
    assert set(ir_doc["IRScreen"]) == {"id", "name", "w", "h", "bg", "nodes"}

    compiled = compile_one(type="switch", w=50, h=30, text="x")
    assert set(ir_doc["IRNode"]["shape"]) - {"control"} == set(compiled) - {"control"}
    assert set(ir_doc["IRControl"]["switch"]) == set(compiled["control"])
    assert set(ir_doc["IRRadius"]["shape"]) == set(compiled["radius"])
    assert set(ir_doc["IRFill"]["solid"]) == set(compiled["fill"])
    assert set(ir_doc["IRText"]["shape"]) == set(comp.text_style({"text": "x"}, "text"))


def test_all_design_cases_compile() -> None:
    paths = sorted(CASES_DIR.glob("*.json"))
    assert len(paths) == 11
    for path in paths:
        design = json.loads(path.read_text(encoding="utf-8"))
        ir = comp.compile_design(design)
        assert ir["version"] == comp.IR_VERSION
        assert ir["screens"], path.name
        for screen in ir["screens"]:
            assert screen["bg"] == ir["bg"]
            for item in comp.iter_nodes(screen["nodes"]):
                assert set(item) >= REQUIRED_NODE_KEYS, (path.name, item.get("id"))
                assert item["w"] > 0 and item["h"] > 0
                assert set(item["radius"]) == {"tl", "tr", "br", "bl"}
