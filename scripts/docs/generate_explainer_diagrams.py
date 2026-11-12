"""Generate deterministic, self-contained SVG explanations for the public docs.

Run from any directory with Python's standard library.  --check validates that
the committed assets match the generator without modifying files.
"""

from __future__ import annotations

import argparse
from html import escape
from pathlib import Path
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "docs" / "images"
INK = "#14263f"
MUTED = "#52647b"
BLUE = "#2563eb"
TEAL = "#0f766e"
AMBER = "#a45b13"
LINE = "#d5dfec"
FONT = "Microsoft YaHei UI, Microsoft YaHei, Noto Sans CJK SC, PingFang SC, system-ui, sans-serif"


class Drawing:
    def __init__(self, height: int, title: str, subtitle: str, badge: str):
        self.height = height
        self.parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="{height}" viewBox="0 0 1280 {height}" role="img" aria-labelledby="title desc">',
            f"<title id=\"title\">{escape(title)}</title>",
            f"<desc id=\"desc\">{escape(subtitle)}</desc>",
            "<defs>",
        ]
        for name, color in (("blue", BLUE), ("teal", TEAL), ("amber", AMBER)):
            self.parts.append(
                f'<marker id="{name}" markerWidth="9" markerHeight="9" refX="8" refY="4.5" orient="auto-start-reverse" markerUnits="strokeWidth">'
                f'<path d="M 0 0 L 8 4.5 L 0 9 Z" fill="{color}"/></marker>'
            )
        self.parts.extend(["</defs>", f'<g font-family="{FONT}">'])
        self.rect(0, 0, 1280, height, fill="#f5f8fc", stroke="none", radius=0)
        self.text(48, 57, title, 29, weight=700)
        self.text(48, 88, subtitle, 17, color=MUTED)
        self.rect(1032, 32, 200, 36, fill="#e7efff", stroke="none", radius=18)
        self.text(1132, 56, badge, 16, color=BLUE, weight=600, anchor="middle")
        self.edge("M 48 112 H 1232", color=LINE, marker=None, width=1)

    def rect(self, x, y, w, h, *, fill="white", stroke=LINE, radius=16):
        self.parts.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" fill="{fill}" stroke="{stroke}"/>'
        )

    def text(self, x, y, value, size=18, *, color=INK, weight=400, anchor="start"):
        self.parts.append(
            f'<text x="{x}" y="{y}" font-size="{size}" fill="{color}" font-weight="{weight}" text-anchor="{anchor}">{escape(value)}</text>'
        )

    def lines(self, x, y, values, size=18, *, color=MUTED, step=28):
        for n, value in enumerate(values):
            self.text(x, y + n * step, value, size, color=color)

    def edge(self, path, *, color=BLUE, marker="blue", both=False, dashed=False, width=2.3):
        end = f' marker-end="url(#{marker})"' if marker else ""
        start = f' marker-start="url(#{marker})"' if marker and both else ""
        dash = ' stroke-dasharray="7 6"' if dashed else ""
        self.parts.append(f'<path d="{path}" fill="none" stroke="{color}" stroke-width="{width}"{end}{start}{dash}/>')

    def label(self, x, y, value, *, color=MUTED, size=16, width=156):
        self.rect(x - width / 2, y - 18, width, 25, fill="#f5f8fc", stroke="none", radius=5)
        self.text(x, y, value, size, color=color, anchor="middle")

    def card(self, x, y, w, h, title, body, *, color=BLUE, size=21):
        self.rect(x, y, w, h)
        self.rect(x + 18, y + 18, 4, h - 36, fill=color, stroke="none", radius=2)
        self.text(x + 36, y + 39, title, size, weight=700)
        self.lines(x + 36, y + 72, body, 17, step=26)

    def footer(self, text):
        self.text(48, self.height - 30, text, 16, color=MUTED)

    def svg(self):
        result = "\n".join(self.parts + ["</g>", "</svg>", ""])
        ET.fromstring(result)
        return result


def evolution():
    d = Drawing(790, "项目如何演化", "三次方案选择：减少重复采集，同时保留问题分析能力。", "需求与方案取舍")
    d.edge("M 408 156 H 455")
    d.label(432, 143, "复用采集与展示", color=BLUE, width=166)
    d.edge("M 824 156 H 871", color=TEAL, marker="teal")
    d.label(848, 143, "保留分析深度", color=TEAL, width=166)
    stages = [
        (48, "01", "自建运维平台", "先验证采集、分析与权限闭环", BLUE,
         ["直接采集数据库与主机状态", "形成权限、事件与分析闭环"],
         ["接入与自建采集配置较重", "界面信息多，分析结论不直观"],
         ["权限、调查、证据与报告主干"]),
        (464, "02", "Grafana 插件探索", "尝试复用成熟监控与看板", BLUE,
         ["复用 Zabbix 数据和 Grafana 图表", "将诊断工作整合到一个入口"],
         ["本次实现偏手动查看与分析", "展示复用后，结论深度仍不足"],
         ["数据接入思路、可视化与实验"]),
        (880, "03", "Zabbix + 原平台", "采集做减法，分析与报告保留", TEAL,
         ["Zabbix 统一采集，按项聚合", "原平台持续发现并联合分析"],
         ["选监控项，不重复配置采集", "结论在前，证据与图表按需看"],
         ["业务权限、巡检与可追溯报告"]),
    ]
    for x, number, title, subtitle, color, goals, problems, kept in stages:
        d.rect(x, 178, 352, 480, stroke="#bcd2f5" if number != "03" else "#9fccc5")
        d.rect(x + 22, 200, 45, 31, fill="#e8f0ff" if number != "03" else "#e2f3ef", stroke="none", radius=9)
        d.text(x + 44.5, 222, number, 17, color=color, weight=700, anchor="middle")
        d.text(x + 24, 271, title, 25, weight=700)
        d.text(x + 24, 302, subtitle, 16, color=color)
        d.text(x + 24, 351, "阶段目标", 17, color=color, weight=700)
        d.lines(x + 24, 381, goals, 17, step=27)
        d.text(x + 24, 460, "遇到的问题" if number != "03" else "当前取舍", 17, color=color, weight=700)
        d.lines(x + 24, 490, problems, 17, step=27)
        d.text(x + 24, 578, "保留下来", 17, color=color, weight=700)
        d.lines(x + 24, 609, kept, 17)
    d.rect(48, 683, 1184, 51, fill="#e8f3f0", stroke="#c2ded6", radius=12)
    d.text(72, 716, "最终方向：让成熟工具负责采集，让平台把分散线索整理成结论、建议与可核对的证据。", 19, color=TEAL, weight=600)
    d.footer("这里记录本项目的实现取舍；不表示 Grafana 的插件能力只能做到这些。")
    return d.svg()


def architecture():
    d = Drawing(1110, "当前系统架构", "已有监控负责采集；平台负责范围授权、持续检查、证据组织与分析。", "数据与访问边界")
    d.rect(48, 140, 1184, 166, fill="#edf2f8", stroke="#d5dfec", radius=20)
    d.text(72, 166, "已有环境与访问入口", 16, color=MUTED, weight=600)
    d.card(72, 187, 260, 96, "被监控对象", ["MySQL · Linux · 应用"], color=BLUE)
    d.card(430, 187, 290, 96, "Zabbix", ["指标、日志与诊断历史"], color=BLUE)
    d.card(900, 187, 308, 96, "Web 界面", ["React / ECharts · 用户操作"], color=BLUE)
    d.edge("M 332 235 H 420")
    d.label(378, 222, "已有采集", width=78, size=15)

    d.rect(48, 364, 1184, 514, fill="#edf5ff", stroke="#b9d0ef", radius=22)
    d.text(72, 401, "平台后台", 24, weight=700)
    d.text(397, 400, "独立控制库与后台进程", 17, color=MUTED)
    d.card(72, 447, 300, 132, "采集 worker", ["定时取数 · 信号检查", "调查调度 · 记录采样缺口"])
    d.card(470, 447, 330, 132, "控制 MySQL", ["对象与权限 · 采样与证据", "任务队列 · 报告与审计"])
    d.card(900, 447, 308, 132, "FastAPI API", ["登录与范围授权", "接入配置 · 数据与报告查询"])
    d.card(72, 687, 300, 132, "AI worker", ["领取调查任务 · 冻结窗口", "组织证据 · 校验并保存报告"], color=TEAL)
    d.card(470, 687, 330, 132, "受限证据工具", ["只读 · 当前授权 · 冻结窗口", "脱敏摘要 · 来源与时间限制"], color=TEAL)

    d.edge("M 575 283 V 331 H 222 V 437")
    d.label(372, 320, "所选 host / item 取数", width=206)
    d.edge("M 372 513 H 460")
    d.label(421, 500, "记录与排队", size=15, width=94)
    d.edge("M 810 513 H 890", both=True)
    d.label(850, 500, "授权查询", size=15, width=80)
    d.edge("M 1054 283 V 437", both=True)
    d.label(1092, 344, "登录 / 配置 / 查看", width=180)
    d.edge("M 900 479 H 850 V 235 H 730", both=True)
    d.label(850, 318, "发现 / 当前历史", width=156)
    d.edge("M 520 589 V 632 H 222 V 677", color=TEAL, marker="teal", both=True)
    d.label(356, 621, "调查任务 / 分析结果", color=TEAL, width=194)
    d.edge("M 372 753 H 460", color=TEAL, marker="teal", both=True)
    d.label(421, 740, "受限工具", color=TEAL, size=15, width=80)
    d.edge("M 702 677 V 589", color=TEAL, marker="teal", both=True)
    d.label(702, 646, "只读取证", color=TEAL, width=98)

    d.card(900, 945, 308, 102, "DeepSeek API（可选）", ["仅获准的脱敏上下文"], color=AMBER, size=20)
    d.edge("M 222 829 V 914 H 1054 V 935", color=AMBER, marker="amber", both=True, dashed=True)
    d.label(642, 904, "按授权与预算调用；结构化结果返回 AI worker", color=AMBER, width=468)
    d.text(48, 1022, "Zabbix 保留上游历史；平台只保存分析所需采样与证据。", 17, color=MUTED)
    d.text(48, 1050, "浏览器与模型均不直连业务数据库；AI 不执行 SQL、Shell 或数据库变更。", 17, color=MUTED)
    d.footer("蓝线：取数与访问    青线：调查与证据    虚线：可选模型调用；两类 worker 的队列独立。")
    return d.svg()


def workflow():
    d = Drawing(850, "从监控数据到问题结论", "后台持续运行；人员先看结论，再核对依据并决定处理。", "持续分析流程")
    d.text(48, 153, "用户选范围 → 采集 worker 取数、检查并调度 → 控制库排队", 17, color=MUTED)
    top = [
        (48, "01", "选择主机与监控项", ["同一业务 · 同一 Zabbix 来源", "组合为联合分析对象"]),
        (354, "02", "后台定时取数", ["读取所选数据", "保留时间、单位与缺口"]),
        (660, "03", "信号或周期检查", ["新异常 / 定期综合检查", "遵守权限与预算"]),
        (966, "04", "调查任务入队", ["去重并记录状态", "保存到控制 MySQL"]),
    ]
    bottom = [
        (966, "05", "冻结分析窗口", ["AI worker 领取调查", "保存当次证据与覆盖情况"]),
        (660, "06", "规则 / 可选 AI", ["受限只读工具与脱敏摘要", "关联线索并生成解释"]),
        (354, "07", "结论与依据", ["现象 · 原因 · 建议", "图表、关系与证据引用"]),
        (48, "08", "人工核对与复验", ["人员决定并实施处理", "再看后续真实采样"]),
    ]
    for y, rows, color in ((190, top, BLUE), (470, bottom, TEAL)):
        for x, number, title, body in rows:
            d.rect(x, y, 266, 152)
            d.text(x + 20, y + 31, number, 16, color=color, weight=700)
            d.text(x + 20, y + 66, title, 20, weight=700)
            d.lines(x + 20, y + 102, body, 16, step=26)
    for x in (314, 620, 926):
        d.edge(f"M {x + 5} 266 H {x + 30}")
    d.edge("M 1099 352 V 460", color=TEAL, marker="teal")
    d.label(1099, 412, "AI worker 领取", color=TEAL, width=154)
    for x in (966, 660, 354):
        d.edge(f"M {x - 5} 546 H {x - 30}", color=TEAL, marker="teal")
    d.text(48, 673, "不能确定或不能完成时，明确显示原因：", 18, weight=600)
    for x, title, body in (
        (48, "缺数 / 过期", "标记未知，不补零或假定正常"),
        (454, "预算 / 权限不足", "显示阻断原因，不偷偷扩大范围"),
        (860, "模型 / 引用校验失败", "保留失败，不拼造替代结论"),
    ):
        d.rect(x, 694, 372, 82, fill="#fff7ea", stroke="#e8d5b7", radius=13)
        d.text(x + 20, 724, title, 18, color=AMBER, weight=700)
        d.text(x + 20, 754, body, 16, color=MUTED)
    d.footer("复验依据后续真实采样；旧报告保持原分析窗口。自动化负责检查，数据库操作由人员控制。")
    return d.svg()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check committed SVGs without writing")
    args = parser.parse_args()
    assets = {
        "project-evolution.svg": evolution(),
        "platform-architecture.svg": architecture(),
        "analysis-workflow.svg": workflow(),
    }
    if not args.check:
        OUTPUT.mkdir(parents=True, exist_ok=True)
    for name, data in assets.items():
        path = OUTPUT / name
        if args.check:
            if not path.is_file() or path.read_text(encoding="utf-8") != data:
                raise SystemExit(f"SVG differs: {name}")
        else:
            path.write_text(data, encoding="utf-8", newline="\n")
        svg = ET.fromstring(data)
        assert svg.get("width") == "1280"
        print(f"{name}: {svg.get('width')} x {svg.get('height')} (XML valid)")


if __name__ == "__main__":
    main()
