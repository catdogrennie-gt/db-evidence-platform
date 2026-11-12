"""Generate the compact architecture diagram with Python's standard library.

``layout()`` is shared with the private PDF exporter.  Coordinates use an SVG
top-left origin; text y positions are baselines.  The public output contains
only simple SVG primitives and no external resources.
"""

from __future__ import annotations

import argparse
from html import escape
from pathlib import Path
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "docs" / "images" / "platform-architecture-wide.svg"
FONT = "Microsoft YaHei, Noto Sans CJK SC, PingFang SC, sans-serif"
INK = "#173a52"
TEAL = "#247c86"
MUTED = "#586b79"
LINE = "#c8d9e1"


def layout():
    """Return width, height and rect/text/line/polyline/polygon primitives."""
    shapes = []

    def rect(x, y, width, height, *, fill="white", stroke=LINE, rx=14):
        shapes.append({"type": "rect", "x": x, "y": y, "w": width,
                       "h": height, "rx": rx, "fill": fill,
                       "stroke": stroke, "strokeWidth": 2})

    def text(x, y, value, size=23, *, fill=MUTED, weight=400, anchor="middle"):
        shapes.append({"type": "text", "x": x, "y": y, "text": value,
                       "fontSize": size, "weight": weight, "anchor": anchor,
                       "fill": fill})

    def line(x1, y1, x2, y2, *, color=TEAL, width=2.5):
        shapes.append({"type": "line", "x1": x1, "y1": y1, "x2": x2,
                       "y2": y2, "stroke": color, "strokeWidth": width})

    def arrow(x1, x2, y):
        line(x1, y, x2 - 10, y)
        shapes.append({"type": "polygon",
                       "points": [[x2 - 11, y - 6], [x2, y], [x2 - 11, y + 6]],
                       "fill": TEAL, "stroke": "none", "strokeWidth": 0})

    rect(0, 0, 1400, 350, stroke="none", rx=0)
    cards = [
        (24, "监控对象", "数据库 / 服务器", "已配置的日志"),
        (302, "Zabbix", "采集与保存", "只读 API 接入"),
        (580, "Python 后端", "FastAPI", "取数整理 · 定时检查"),
        (858, "AI 分析", "DeepSeek API", "关联原因 · 处理建议"),
        (1136, "前端展示", "React / ECharts", "结论 · 图表 · 依据"),
    ]
    for index, (x, title, first, second) in enumerate(cards):
        rect(x, 28, 240, 154, fill="#f2f8f8" if index == 3 else "#f7fafc")
        rect(x + 16, 47, 4, 113, fill=TEAL, stroke="none", rx=2)
        text(x + 128, 76, title, 28, fill=INK, weight=700)
        text(x + 128, 113, first)
        text(x + 128, 147, second, 22)
        if index < len(cards) - 1:
            arrow(x + 246, x + 272, 105)

    # These edges describe stored inputs and outputs, not model-side DB access.
    line(700, 182, 700, 226, color=LINE)
    line(978, 182, 978, 226, color=LINE)
    rect(580, 226, 518, 78, fill="#eef5f7")
    text(839, 257, "平台 MySQL", 25, fill=INK, weight=700)
    text(839, 288, "配置 · 任务 · 分析数据 · 报告", 22)
    text(1256, 218, "由后端返回结果", 22, fill=TEAL)
    text(24, 260, "流程图中的箭头表示处理顺序", 20, anchor="start")
    text(24, 289, "页面通过后端接口查询数据", 20, anchor="start")
    text(700, 336, "授权只读数据 · 脱敏后分析 · AI 无 SQL / Shell 执行权限", 22, fill=TEAL)
    return {"width": 1400, "height": 350, "shapes": shapes}


def svg():
    diagram = layout()
    width, height = diagram["width"], diagram["height"]
    result = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">',
        '<title id="title">DB Evidence Platform：从监控到问题分析</title>',
        '<desc id="desc">Zabbix 采集并提供只读 API，Python 后端取数与检查，DeepSeek 辅助分析，React 和 ECharts 经后端接口展示结果。平台 MySQL 保存配置、任务、分析数据和报告。AI 没有 SQL 或 Shell 执行权限。箭头表示处理顺序。</desc>',
    ]
    for original in diagram["shapes"]:
        shape = original.copy()
        kind = shape.pop("type")
        value = shape.pop("text", None)
        if kind == "text":
            shape["font-family"] = FONT
        renames = {"w": "width", "h": "height", "strokeWidth": "stroke-width", "fontSize": "font-size",
                   "weight": "font-weight", "anchor": "text-anchor"}
        attrs = []
        for name, attribute in shape.items():
            if name == "points":
                attribute = " ".join(f"{x},{y}" for x, y in attribute)
            attrs.append(f'{renames.get(name, name)}="{escape(str(attribute), quote=True)}"')
        if value is not None:
            result.append(f'<{kind} {" ".join(attrs)}>{escape(value)}</{kind}>')
        else:
            result.append(f'<{kind} {" ".join(attrs)}/>')
    result.extend(["</svg>", ""])
    rendered = "\n".join(result)
    ET.fromstring(rendered)
    return rendered


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check without writing files.")
    args = parser.parse_args()
    content = svg()
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_text(encoding="utf-8") != content:
            raise SystemExit("Compact architecture SVG is missing or out of date.")
        print("Compact architecture SVG matches generator (1400 x 350).")
    else:
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(content, encoding="utf-8")
        print("Wrote docs/images/platform-architecture-wide.svg (1400 x 350).")


if __name__ == "__main__":
    main()
