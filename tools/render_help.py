"""用 Pillow 生成 `/stpro help` 使用的完整导航图。"""

from __future__ import annotations

import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
except Exception:  # 独立运行渲染脚本时 AstrBot 可能不在 import path
    get_astrbot_data_path = None

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "assets" / "stpro_help.png"
BUILTIN_FONT = ROOT / "assets" / "fonts" / "NotoSansSC-VF.ttf"
WIDTH = 1600
MARGIN = 64
GAP = 18

COLORS = {
    "background": "#F4F8FA",
    "surface": "#FFFFFF",
    "text": "#14313F",
    "muted": "#637985",
    "hint": "#81939C",
    "line": "#DDE9EE",
    "cyan": "#0588B7",
    "code": "#0876AA",
    "code_bg": "#F5FBFE",
    "amber": "#9A5A08",
    "amber_bg": "#FFF7E9",
}

_DATA_DIR = (
    Path(get_astrbot_data_path()) if get_astrbot_data_path else ROOT.parent / "data"
)
FONT_CANDIDATES = [
    BUILTIN_FONT,
    _DATA_DIR / "font.ttf",
    _DATA_DIR / "font-bold.ttf",
    Path(r"C:\Windows\Fonts\msyh.ttc"),
    Path(r"C:\Windows\Fonts\msyhbd.ttc"),
    Path(r"C:\Windows\Fonts\simhei.ttf"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.otf"),
    Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
    Path("/System/Library/Fonts/Hiragino Sans GB.ttc"),
    Path("/System/Library/Fonts/PingFang.ttc"),
]

COMMAND_ROWS = [
    [
        ("查看帮助", "发送这张指令与设置导航图。", "/stpro help"),
        ("创建档案", "保存接口与密钥，并获取模型。", "/stpro new <配置名>"),
        ("查看档案", "查看档案列表或指定档案详情。", "/stpro list [配置名]"),
        (
            "修改接口",
            "修改地址、密钥或同时修改。",
            "/stpro set <配置名> {endpoint|apikey|all}",
        ),
        ("删除档案", "删除前需要完成二次确认。", "/stpro remove <配置名>"),
    ],
    [
        ("选择模型", "支持模型序号或完整名称。", "/stpro model <配置名> [序号或名称]"),
        ("创建人格", "新建当前档案专用人格。", "/stpro persona <配置名> new [人格名]"),
        (
            "切换人格",
            "选择当前档案使用的人格。",
            "/stpro persona <配置名> set [人格名]",
        ),
        ("修改人格", "无人格名时列出可用人格。", "/stpro persona <配置名> [人格名]"),
        (
            "删除人格",
            "删除当前档案保存的人格。",
            "/stpro persona <配置名> del [人格名]",
        ),
    ],
    [
        ("绑定群聊", "群内可省略群号，私聊需填写。", "/stpro bind <配置名> [群号]"),
        ("解除绑定", "解除配置，不会让机器人退群。", "/stpro unbind [群号]"),
        ("检查状态", "检查指定档案或全部档案。", "/stpro update [配置名]"),
    ],
]

SETTING_SECTIONS = [
    (
        "模型、人格与上下文",
        [
            (
                "对话模型",
                "当前：由档案选择",
                "候选：执行 /stpro model <配置名> 动态获取",
                "",
            ),
            (
                "对话人格",
                "当前：default",
                "候选：当前档案保存的人格 + default 配置当前人格",
                "",
            ),
            (
                "压缩前最多保留对话轮数",
                "当前：-1",
                "-1 表示不按轮数限制",
                "/stpro settings <配置名> context-turns <数量>",
            ),
            (
                "上下文溢出策略",
                "当前：llm_compress",
                "可选：compress（压缩） / truncate（截断）",
                "/stpro settings <配置名> overflow-strategy <compress|truncate>",
            ),
        ],
    ),
    (
        "群聊与平台行为",
        [
            (
                "隔离对话",
                "当前：关闭",
                "可选：on / off",
                "/stpro settings <配置名> unique-session [on|off]",
            ),
            (
                "回复时 @ 发送人",
                "当前：开启",
                "可选：on / off",
                "/stpro settings <配置名> reply-mention [on|off]",
            ),
            (
                "回复时引用消息",
                "当前：关闭",
                "可选：on / off",
                "/stpro settings <配置名> reply-quote [on|off]",
            ),
            (
                "群聊消息记录注入上下文",
                "当前：关闭",
                "可选：on / off",
                "/stpro settings <配置名> group-context [on|off]",
            ),
            (
                "注入上下文最大消息数量",
                "当前：20",
                "填写非负整数",
                "/stpro settings <配置名> group-context-count <数量>",
            ),
            (
                "自动理解图片",
                "当前：关闭",
                "可选：on / off；需管理员配置图片转述模型",
                "/stpro settings <配置名> group-image-caption [on|off]",
            ),
            (
                "主动回复",
                "当前：关闭",
                "可选：on / off",
                "/stpro settings <配置名> active-reply [on|off]",
            ),
        ],
    ),
    (
        "输出与消息呈现",
        [
            (
                "文本转图像输出",
                "当前：关闭",
                "可选：on / off",
                "/stpro settings <配置名> t2i [on|off]",
            ),
            (
                "文本转图像字数阈值",
                "当前：150",
                "达到该字数后触发图片输出",
                "/stpro settings <配置名> t2i-threshold <字数>",
            ),
            (
                "启用分段回复",
                "当前：关闭",
                "可选：on / off",
                "/stpro settings <配置名> segmented-reply [on|off]",
            ),
            (
                "仅对 LLM 结果分段",
                "当前：开启",
                "可选：on / off",
                "/stpro settings <配置名> segment-llm-only [on|off]",
            ),
            (
                "间隔方法",
                "当前：random",
                "可选：random（随机） / log（按消息长度计算）",
                "/stpro settings <配置名> segment-interval-method <random|log>",
            ),
            (
                "随机间隔时间",
                "当前：1.5,3.5 秒",
                "格式：最小值,最大值",
                "/stpro settings <配置名> segment-interval <最小值,最大值>",
            ),
            (
                "分段回复字数阈值",
                "当前：150",
                "超过该长度的消息不再分段",
                "/stpro settings <配置名> segment-threshold <字数>",
            ),
        ],
    ),
]


def _font_path() -> Path:
    for candidate in FONT_CANDIDATES:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("未找到可显示中文的字体")


def font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    loaded = ImageFont.truetype(str(_font_path()), size=size)
    if BUILTIN_FONT.is_file():
        try:
            loaded.set_variation_by_name("Bold" if bold else "Regular")
        except (AttributeError, OSError, ValueError):
            pass
    return loaded


def _plugin_version() -> str:
    try:
        text = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
        match = re.search(r"^version:\s*[vV]?([^\s#]+)", text, re.MULTILINE)
        if match:
            return match.group(1)
    except OSError:
        pass
    return "unknown"


def _astrbot_version() -> str:
    try:
        from astrbot import __version__

        return str(__version__).removeprefix("v")
    except Exception:
        return "unknown"


def _rounded(draw, box, radius, fill, outline=None, width=1) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def _fit_text(draw, text, max_width, *, size, min_size=15, bold=False):
    for current in range(size, min_size - 1, -1):
        candidate = font(current, bold=bold)
        if draw.textbbox((0, 0), text, font=candidate)[2] <= max_width:
            return candidate
    return font(min_size, bold=bold)


def _draw_command_card(draw, box, item) -> None:
    x0, y0, x1, y1 = box
    title, description, command = item
    _rounded(draw, box, 14, COLORS["surface"], COLORS["line"], 2)
    draw.text((x0 + 17, y0 + 15), title, font=font(22, bold=True), fill=COLORS["text"])
    draw.text(
        (x0 + 17, y0 + 52),
        description,
        font=_fit_text(draw, description, x1 - x0 - 34, size=16, min_size=13),
        fill=COLORS["muted"],
    )
    code_box = (x0 + 14, y1 - 55, x1 - 14, y1 - 14)
    _rounded(draw, code_box, 8, COLORS["code_bg"], "#CFE5F1")
    draw.text(
        (code_box[0] + 9, code_box[1] + 10),
        command,
        font=_fit_text(
            draw, command, code_box[2] - code_box[0] - 18, size=15, min_size=10
        ),
        fill=COLORS["code"],
    )


def _draw_setting_card(draw, box, item) -> None:
    x0, y0, x1, y1 = box
    name, current, options, command = item
    _rounded(draw, box, 13, COLORS["surface"], COLORS["line"], 2)
    draw.text((x0 + 17, y0 + 14), name, font=font(19, bold=True), fill=COLORS["text"])
    draw.text(
        (x0 + 17, y0 + 47),
        current,
        font=_fit_text(draw, current, x1 - x0 - 34, size=16, min_size=13),
        fill=COLORS["cyan"],
    )
    draw.text(
        (x0 + 17, y0 + 75),
        options,
        font=_fit_text(draw, options, x1 - x0 - 34, size=14, min_size=10),
        fill=COLORS["muted"],
    )
    if command:
        command_box = (x0 + 14, y1 - 49, x1 - 14, y1 - 12)
        _rounded(draw, command_box, 7, COLORS["amber_bg"])
        draw.text(
            (command_box[0] + 8, command_box[1] + 9),
            command,
            font=_fit_text(
                draw, command, command_box[2] - command_box[0] - 16, size=13, min_size=9
            ),
            fill=COLORS["amber"],
        )


def render(output: Path = OUTPUT) -> Path:
    command_height = 68 + len(COMMAND_ROWS) * 142 + (len(COMMAND_ROWS) - 1) * GAP + 28
    setting_height = sum(
        62
        + ((len(items) + 1) // 2) * 142
        + max(0, ((len(items) + 1) // 2) - 1) * GAP
        + 24
        for _, items in SETTING_SECTIONS
    )
    footer_height = 74
    height = 172 + command_height + setting_height + footer_height
    canvas = Image.new("RGB", (WIDTH, height), COLORS["background"])
    draw = ImageDraw.Draw(canvas)
    draw.text(
        (MARGIN, 48), "STPRO 独立档案", font=font(42, bold=True), fill=COLORS["text"]
    )
    draw.text(
        (MARGIN, 105),
        "先创建档案、选择模型与人格，再绑定群聊；下方设置按功能分区展示。",
        font=font(20),
        fill=COLORS["muted"],
    )
    note = "< > 为必填参数，[ ] 为可选参数。"
    note_font = font(17)
    note_width = draw.textbbox((0, 0), note, font=note_font)[2]
    draw.text(
        (WIDTH - MARGIN - note_width, 70), note, font=note_font, fill=COLORS["muted"]
    )

    y = 166
    draw.text((MARGIN, y), "档案管理", font=font(28, bold=True), fill=COLORS["text"])
    draw.text(
        (MARGIN + 150, y + 8),
        "按首次使用流程和功能横向排列",
        font=font(16),
        fill=COLORS["hint"],
    )
    y += 58
    command_width = (WIDTH - MARGIN * 2 - GAP * 4) // 5
    for row in COMMAND_ROWS:
        for index, item in enumerate(row):
            x = MARGIN + index * (command_width + GAP)
            _draw_command_card(draw, (x, y, x + command_width, y + 142), item)
        y += 142 + GAP
    y += 22

    setting_width = (WIDTH - MARGIN * 2 - GAP) // 2
    for title, items in SETTING_SECTIONS:
        draw.text((MARGIN, y), title, font=font(28, bold=True), fill=COLORS["text"])
        y += 52
        for index, item in enumerate(items):
            x = MARGIN + (index % 2) * (setting_width + GAP)
            card_y = y + (index // 2) * (142 + GAP)
            _draw_setting_card(draw, (x, card_y, x + setting_width, card_y + 142), item)
        rows = (len(items) + 1) // 2
        y += rows * 142 + max(0, rows - 1) * GAP + 30

    footer = f"Standalone Profile v{_plugin_version()} | AstrBot v{_astrbot_version()}"
    footer_font = font(16)
    footer_width = draw.textbbox((0, 0), footer, font=footer_font)[2]
    draw.text(
        ((WIDTH - footer_width) // 2, height - 48),
        footer,
        font=footer_font,
        fill=COLORS["hint"],
    )

    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output, format="PNG", optimize=True)
    return output


if __name__ == "__main__":
    print(render())
