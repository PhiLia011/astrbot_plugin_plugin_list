"""插件清单：统计展示 AstrBot 已安装的插件，并查看指定插件的指令。

指令：
- ``插件列表``（别名 ``插件清单``）：列出所有已加载插件及其版本、启用状态、指令数。
- ``查看xx插件帮助``：二级页面，展示指定插件的全部指令（无状态，无需会话记录）。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from astrbot.api import html_renderer, star
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.event.filter import CustomFilter
from astrbot.core.star.star import star_registry

PLUGIN_NAME_PREFIX = "astrbot_plugin_"
HELP_PHRASE_RE = re.compile(r"^查看\s*(.+?)\s*插件帮助$")
DESC_MAX_LEN = 100
SUB_DESC_MAX_LEN = 40
MAX_CANDIDATES = 10

# 这些符号在 AstrBot 的本地兜底渲染字体（CJK 字体，无 emoji）里均可正常显示。
OK_MARK = "√"
FAIL_MARK = "×"
BUILTIN_TAG = "[内置]"
DISABLED_TAG = "[已禁用]"
# 渲染器会丢掉行首空白，缩进必须用可见字符
FIELD_PREFIX = "│ "
SUB_PREFIX = "├ "

# 与 _conf_schema.json 保持一致；缺少配置对象（例如离线自检）时用这里的默认值
DEFAULT_CONFIG: dict[str, Any] = {
    "use_remote_template": False,
    "template_name": "",
    "background_image": "assets/bg_user.png",
    "background_overlay": 15,
    "padding": 48,
    "font_size": 0,
    "layout": "auto",
    "columns": 1,
    "text_height_limit": 100,
    "line_spacing": 145,
    "text_color": "#FFFFFF",
    "text_position": "bottom",
    "scrim_opacity": 60,
}

TEXT_POSITIONS = ("top", "center", "bottom")
LAYOUT_MODES = ("auto", "overlay", "below")
COLUMN_GAP = 40


def _config_value(config: Any, key: str) -> Any:
    try:
        return config.get(key, DEFAULT_CONFIG[key])
    except Exception:
        return DEFAULT_CONFIG[key]


def _clamp_int(value: Any, low: int, high: int, fallback: int) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, number))


def resolve_settings(config: Any = None) -> dict[str, Any]:
    """把插件配置规整成渲染参数，越界或非法值一律回落到默认值。"""
    position = str(_config_value(config, "text_position") or "").strip().lower()
    layout = str(_config_value(config, "layout") or "").strip().lower()
    return {
        "use_remote_template": bool(_config_value(config, "use_remote_template")),
        "template_name": str(_config_value(config, "template_name") or "").strip(),
        "background_image": str(_config_value(config, "background_image") or "").strip(),
        "background_overlay": _clamp_int(
            _config_value(config, "background_overlay"), 0, 90, 15
        ),
        "padding": _clamp_int(_config_value(config, "padding"), 0, 240, 48),
        "font_size": _clamp_int(_config_value(config, "font_size"), 0, 96, 0),
        "layout": layout if layout in LAYOUT_MODES else "auto",
        "columns": _clamp_int(_config_value(config, "columns"), 1, 2, 1),
        "text_height_limit": _clamp_int(
            _config_value(config, "text_height_limit"), 30, 100, 100
        ),
        "line_spacing": _clamp_int(
            _config_value(config, "line_spacing"), 100, 240, 145
        ),
        "text_color": str(_config_value(config, "text_color") or "#FFFFFF").strip(),
        "text_position": position if position in TEXT_POSITIONS else "bottom",
        "scrim_opacity": _clamp_int(
            _config_value(config, "scrim_opacity"), 0, 100, 60
        ),
    }


async def load_background(source: str) -> Any | None:
    """加载底图。支持 http(s) 链接、绝对路径、相对插件目录的路径。"""
    if not source:
        return None
    try:
        from PIL import Image

        if source.startswith(("http://", "https://")):
            from astrbot.core.utils.io import download_image_by_url

            return Image.open(await download_image_by_url(source)).convert("RGB")

        path = Path(source)
        if not path.is_absolute():
            path = Path(__file__).resolve().parent / path
        if not path.is_file():
            return None
        return Image.open(path).convert("RGB")
    except Exception:
        return None


def load_font(size: int, bold: bool = False) -> Any | None:
    """加载带中文字形的字体，优先复用 AstrBot 的字体管理。"""
    try:
        from astrbot.core.utils.t2i.local_strategy import FontManager

        return FontManager.get_font(size, bold=bold)
    except Exception:
        pass
    try:
        from PIL import ImageFont

        candidates = (
            r"C:\Windows\Fonts\msyhbd.ttc",
            r"C:\Windows\Fonts\msyh.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "DejaVuSans.ttf",
        )
        for candidate in candidates:
            try:
                return ImageFont.truetype(candidate, size)
            except Exception:
                continue
        return ImageFont.load_default()
    except Exception:
        return None


def hex_to_rgb(value: str, fallback: tuple[int, int, int] = (255, 255, 255)):
    text_value = str(value or "").strip().lstrip("#")
    if len(text_value) == 3:
        text_value = "".join(char * 2 for char in text_value)
    try:
        return tuple(int(text_value[index : index + 2], 16) for index in (0, 2, 4))
    except Exception:
        return fallback


def wrap_line(draw: Any, line: str, font: Any, max_width: int) -> list[str]:
    """按可用宽度折行；中文逐字折，英文尽量在空格处折。"""
    value = (line or "").rstrip()
    if not value:
        return [""]
    if draw.textlength(value, font=font) <= max_width:
        return [value]

    pieces: list[str] = []
    current = ""
    for char in value:
        if current and draw.textlength(current + char, font=font) > max_width:
            head, _, tail = current[-14:].rpartition(" ")
            if head and char != " ":
                pieces.append(current[: -len(current[-14:])] + head)
                current = tail + char
                continue
            pieces.append(current)
            current = char
        else:
            current += char
    if current:
        pieces.append(current)
    return pieces


def layout_block(
    draw: Any,
    text: str,
    size: int,
    line_spacing: float,
    max_width: int,
    columns: int = 1,
    gap: int = COLUMN_GAP,
) -> tuple[list[tuple[str, Any, int]], list[list[tuple[str, Any, int]]], int]:
    """排版：标题占满一行宽度，正文按 columns 列切分。

    Returns:
        (标题行, 各列的行, 总高度)，行元素为 (文本, 字体, 行高)。
    """
    title_font = load_font(size, bold=True)
    body_font = load_font(size)
    if title_font is None or body_font is None:
        return [], [], 0

    title_height = round(size * 1.25 * line_spacing)
    body_height = round(size * line_spacing)
    gap_height = round(size * line_spacing * 0.5)

    lines = [line.rstrip() for line in (text or "").splitlines()]
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return [], [], 0

    title_entries = [
        (piece, title_font, title_height)
        for piece in wrap_line(draw, lines[0], title_font, max_width)
    ]

    column_width = (
        max_width
        if columns <= 1
        else max(120, (max_width - gap * (columns - 1)) // columns)
    )
    body: list[tuple[str, Any, int]] = []
    for line in lines[1:]:
        if not line.strip():
            body.append(("", body_font, gap_height))
            continue
        for piece in wrap_line(draw, line, body_font, column_width):
            body.append((piece, body_font, body_height))

    if columns <= 1:
        column_entries = [body]
    else:
        per_column = max(1, -(-len(body) // columns))
        column_entries = [
            body[index : index + per_column]
            for index in range(0, max(1, len(body)), per_column)
        ]
    if not column_entries:
        column_entries = [[]]

    title_total = sum(item[2] for item in title_entries)
    body_total = max((sum(item[2] for item in column) for column in column_entries), default=0)
    return title_entries, column_entries, title_total + body_total


def extend_canvas(image: Any, target_height: int) -> Any:
    """文字放不下时向下接一段模糊压暗的延展区域，避免挡住主图。"""
    from PIL import Image, ImageFilter

    extra = target_height - image.height
    if extra <= 0:
        return image
    strip = image.crop((0, max(0, image.height - 48), image.width, image.height))
    strip = strip.resize((image.width, extra), Image.LANCZOS).filter(
        ImageFilter.GaussianBlur(16)
    )
    strip = Image.blend(strip, Image.new("RGB", strip.size, (10, 12, 18)), 0.62)
    canvas = Image.new("RGB", (image.width, target_height))
    canvas.paste(image, (0, 0))
    canvas.paste(strip, (0, image.height))
    return canvas


def render_on_background(
    text: str, background: Any, settings: dict[str, Any]
) -> str | None:
    """把文字直接排到图片上（不叠加 AstrBot 的 t2i 卡片层）。

    优先级是“文字先看得清，图片其次”：字号优先按可读性来定，装不下时向下延展画布，
    而不是一味缩小字号把人像露出来。
    """
    try:
        from PIL import Image, ImageDraw

        from astrbot.core.utils.io import save_temp_img

        source = background.convert("RGB")
        width, height = source.size
        padding = settings["padding"]
        max_width = max(80, width - padding * 2)
        spacing = settings["line_spacing"] / 100

        base = settings["font_size"] or max(22, min(48, round(width / 30)))
        floor = max(16, round(base * 0.75))
        region = round((height - padding * 2) * settings["text_height_limit"] / 100)
        # 至少留出一行半，避免限制过小导致永远选不出字号
        region = max(region, round(base * spacing * 1.5))

        probe = ImageDraw.Draw(source)
        columns = settings["columns"]

        def build(size_value: int):
            return layout_block(
                probe, text, size_value, spacing, max_width, columns
            )

        size = base
        title_entries, column_entries, total = build(size)
        if not title_entries and not any(column_entries):
            return None

        mode = settings["layout"]
        place_below = mode == "below"
        if mode == "overlay" and total > region:
            # 只有显式要求“压在图上”时，才为了不越界而缩小字号
            fitted = None
            for candidate in (round(base * 0.9), round(base * 0.8), floor):
                shrunk_title, shrunk_columns, shrunk_total = build(candidate)
                if (shrunk_title or any(shrunk_columns)) and shrunk_total <= region:
                    fitted = (candidate, shrunk_title, shrunk_columns, shrunk_total)
                    break
            if fitted is not None:
                size, title_entries, column_entries, total = fitted
            else:
                place_below = True
        elif mode == "auto" and total > region:
            # 文字优先：保持可读字号，把画布向下延展给文字让位
            place_below = True

        if place_below:
            canvas_height = height + total + padding * 2
        else:
            canvas_height = max(height, total + padding * 2)
        canvas = extend_canvas(source, canvas_height).convert("RGBA")

        overlay = settings["background_overlay"]
        if overlay > 0:
            shade = Image.new("RGBA", canvas.size, (0, 0, 0, round(255 * overlay / 100)))
            canvas = Image.alpha_composite(canvas, shade)

        position = settings["text_position"]
        if place_below:
            start_y = height + padding
        elif position == "top":
            start_y = padding
        elif position == "center":
            start_y = max(padding, (canvas.height - total) // 2)
        else:
            start_y = max(padding, canvas.height - padding - total)

        scrim = settings["scrim_opacity"]
        if scrim > 0:
            top = max(0, start_y - round(size * 0.9))
            bottom = min(canvas.height, start_y + total + round(size * 0.9))
            if bottom > top:
                # 顶部用一小段渐变融进原图，其余均匀压暗，保证文字始终清晰
                ramp = max(1, round((bottom - top) * 0.18))
                mask = Image.new("L", (1, bottom - top), 0)
                for index in range(mask.height):
                    mask.putpixel(
                        (0, index),
                        round(
                            255
                            * scrim
                            / 100
                            * min(1.0, (index / ramp) ** 1.1)
                        ),
                    )
                mask = mask.resize((canvas.width, bottom - top))
                region = canvas.crop((0, top, canvas.width, bottom))
                dark = Image.new("RGBA", region.size, (8, 10, 16, 255))
                canvas.paste(Image.composite(dark, region, mask), (0, top))

        draw = ImageDraw.Draw(canvas)
        color = hex_to_rgb(settings["text_color"])
        stroke = max(1, round(size / 12))

        def paint(content: str, font: Any, x: int, y: int) -> None:
            if content:
                draw.text(
                    (x, y),
                    content,
                    font=font,
                    fill=color + (255,),
                    stroke_width=stroke,
                    stroke_fill=(0, 0, 0, 185),
                )

        y = start_y
        for content, font, line_height in title_entries:
            paint(content, font, padding, y)
            y += line_height

        column_width = (
            max_width
            if columns <= 1
            else max(120, (max_width - COLUMN_GAP * (columns - 1)) // columns)
        )
        for index, column in enumerate(column_entries):
            column_x = padding + index * (column_width + COLUMN_GAP)
            column_y = y
            for content, font, line_height in column:
                paint(content, font, column_x, column_y)
                column_y += line_height
        return save_temp_img(canvas.convert("RGB"))
    except Exception:
        return None

PERMISSION_ZH = {
    "admin": "管理员",
    "member": "所有人",
    "everyone": "所有人",
    "group_admin": "群管理员",
    "shared_group_admin": "群管理员",
}


def parse_help_phrase(message_str: str) -> str | None:
    """从 “查看xx插件帮助” 中取出 xx，不匹配则返回 None。"""
    match = HELP_PHRASE_RE.match((message_str or "").strip())
    if not match:
        return None
    return match.group(1).strip()


def normalize_name(text: str | None) -> str:
    """用于模糊匹配：去掉 astrbot_plugin_ 前缀并忽略大小写。"""
    value = (text or "").strip().lower()
    if value.startswith(PLUGIN_NAME_PREFIX):
        value = value[len(PLUGIN_NAME_PREFIX) :]
    return value


def display_name(metadata: Any) -> str:
    """插件展示名，缺省回退到插件名。"""
    return (metadata.display_name or metadata.name or "未知插件").strip()


def match_plugin(query: str, plugins: list[Any]) -> tuple[Any | None, list[Any]]:
    """按关键字匹配插件。

    Returns:
        (命中的插件元数据, 候选列表)。命中唯一插件时候选列表为空；
        未命中或存在歧义时命中的插件为 None。
    """
    keyword = (query or "").strip()
    if not keyword:
        return None, []

    keyword_lower = keyword.lower()
    keyword_normalized = normalize_name(keyword)

    def keys(metadata: Any) -> list[str]:
        return [
            value
            for value in (
                metadata.display_name,
                metadata.name,
                metadata.root_dir_name,
            )
            if value
        ]

    exact = [
        metadata
        for metadata in plugins
        if any(key.strip().lower() == keyword_lower for key in keys(metadata))
    ]
    if not exact:
        exact = [
            metadata
            for metadata in plugins
            if any(normalize_name(key) == keyword_normalized for key in keys(metadata))
        ]
    if len(exact) == 1:
        return exact[0], []
    if len(exact) > 1:
        return None, exact

    fuzzy = [
        metadata
        for metadata in plugins
        if keyword_normalized
        and any(
            keyword_normalized in normalize_name(key) for key in keys(metadata)
        )
    ]
    if len(fuzzy) == 1:
        return fuzzy[0], []
    return None, fuzzy


def astrbot_version() -> str:
    try:
        from astrbot.core.config.default import VERSION

        return str(VERSION)
    except Exception:
        return "?"


def version_text(version: str | None) -> str:
    """版本号统一展示，避免出现 ``vv0.3`` 这类重复前缀。"""
    value = (version or "").strip() or "?"
    if value[:1] in ("v", "V"):
        value = value[1:].strip()
    return value or "?"


async def render_image(text: str, config: Any = None) -> Any | None:
    """用 AstrBot 内置文转图能力把文本渲染成图片组件。

    配置了底图时，文字会由本插件自己排版、直接画在底图上（不叠加核心的 t2i 卡片层）；
    没有底图时回退到核心的本地渲染。开启 ``use_remote_template`` 后优先走远端 HTML 模板，
    远端不可用时自动回落。失败时返回 None，由调用方回退为纯文本。
    """
    settings = resolve_settings(config)
    try:
        from astrbot.core.message.components import Image

        if settings["use_remote_template"]:
            rendered = await core_render(
                text, use_network=True, template_name=settings["template_name"] or None
            )
            if rendered and str(rendered).startswith("http"):
                # 远端模板渲染成功，直接使用模板效果
                return Image.fromURL(str(rendered))

        background = await load_background(settings["background_image"])
        if background is not None:
            staged = render_on_background(text, background, settings)
            if staged:
                return Image.fromFileSystem(staged)

        rendered = await core_render(text, use_network=False, template_name=None)
        return Image.fromFileSystem(str(rendered)) if rendered else None
    except Exception:
        return None


async def core_render(
    text: str, *, use_network: bool, template_name: str | None
) -> str | None:
    """调用 AstrBot 内置文转图；失败时返回 None。"""
    try:
        return await html_renderer.render_t2i(
            text,
            use_network=use_network,
            return_url=True,
            template_name=template_name,
        )
    except Exception:
        return None


def command_params(handler_full_name: str | None) -> str:
    """取指令的参数签名，例如 ``player(str),count(int)=3``。"""
    if not handler_full_name:
        return ""
    try:
        from astrbot.core.star.filter.command import CommandFilter
        from astrbot.core.star.star_handler import star_handlers_registry

        handler = star_handlers_registry.get_handler_by_full_name(handler_full_name)
        if handler is None:
            return ""
        for event_filter in handler.event_filters:
            if isinstance(event_filter, CommandFilter):
                return event_filter.print_types() or ""
    except Exception:
        return ""
    return ""


def permission_text(permission: str | None) -> str:
    value = (permission or "").strip()
    return PERMISSION_ZH.get(value, value or "所有人")


def first_line(text: str | None) -> str:
    value = (text or "").strip().replace("\n", " ")
    return value


def format_command(command: dict[str, Any], index: int) -> str:
    """格式化一条顶层指令（指令组会展开子指令）。"""
    name = (
        command.get("effective_command")
        or command.get("original_command")
        or command.get("handler_name")
        or "未知指令"
    )
    is_group = command.get("type") == "group"

    # 用 [n] 而不是 "n."：本地渲染器会把 "10." 当成有序列表标记，只预留 30px 导致串行
    lines = [f"[{index}] {name}" + ("（指令组）" if is_group else "")]
    if not command.get("enabled", True):
        lines[0] += f" {DISABLED_TAG}"

    aliases = [alias for alias in (command.get("aliases") or []) if alias]
    if aliases:
        lines.append(f"{FIELD_PREFIX}别名：{'、'.join(aliases)}")
    if not is_group:
        params = command_params(command.get("handler_full_name"))
        lines.append(f"{FIELD_PREFIX}参数：{params or '无'}")
    lines.append(
        f"{FIELD_PREFIX}权限：{permission_text(command.get('permission'))}"
    )
    description = first_line(command.get("description"))
    if description:
        lines.append(f"{FIELD_PREFIX}说明：{description}")

    for sub_command in command.get("sub_commands") or []:
        lines.append(format_sub_command(sub_command))
    return "\n".join(lines)


def format_sub_command(command: dict[str, Any]) -> str:
    name = (
        command.get("effective_command")
        or command.get("handler_name")
        or "未知指令"
    )
    params = command_params(command.get("handler_full_name"))
    line = (
        f"{SUB_PREFIX}{name}｜参数：{params or '无'}"
        f"｜权限：{permission_text(command.get('permission'))}"
    )
    if not command.get("enabled", True):
        line += f" {DISABLED_TAG}"
    description = first_line(command.get("description"))
    if description:
        if len(description) > SUB_DESC_MAX_LEN:
            description = description[:SUB_DESC_MAX_LEN] + "…"
        line += f"｜{description}"
    return line


def build_list_text(
    plugins: list[Any],
    commands_by_plugin: dict[str, list[dict[str, Any]]],
    wake_prefix: str,
    commands_error: str | None = None,
) -> str:
    """生成插件清单文本。"""
    third_party = [item for item in plugins if not item.reserved]
    builtin = [item for item in plugins if item.reserved]
    third_party.sort(key=lambda item: display_name(item).lower())
    builtin.sort(key=lambda item: display_name(item).lower())
    ordered = third_party + builtin

    total = len(ordered)
    enabled = sum(1 for item in ordered if item.activated)
    lines = [
        f"插件清单 · AstrBot v{astrbot_version()}",
        f"共 {total} 个（第三方 {len(third_party)} / 内置 {len(builtin)}）"
        f"｜{OK_MARK} 启用 {enabled} ｜{FAIL_MARK} 禁用 {total - enabled}",
        "",
    ]

    for metadata in ordered:
        status = OK_MARK if metadata.activated else FAIL_MARK
        tag = f"{BUILTIN_TAG} " if metadata.reserved else ""
        command_count = len(commands_by_plugin.get(metadata.name or "", []))
        lines.append(
            f" {status} {tag}{display_name(metadata)}"
            f" · v{version_text(metadata.version)} · {command_count} 个指令"
        )

    if commands_error:
        lines.extend(["", f"读取指令列表失败：{commands_error}"])

    example_source = third_party or ordered
    # 优先用带展示名的插件举例，避免示例里出现 astrbot_plugin_xxx 这类模块名
    example = next(
        (display_name(item) for item in example_source if item.display_name),
        display_name(example_source[0]) if example_source else "插件名",
    )
    lines.extend(
        [
            "",
            f"发送 {wake_prefix}查看xx插件帮助 查看某插件的指令，"
            f"例如 {wake_prefix}查看{example}插件帮助",
        ]
    )
    return "\n".join(lines)


def build_detail_text(
    metadata: Any,
    commands: list[dict[str, Any]],
    wake_prefix: str,
    commands_error: str | None = None,
) -> str:
    """生成插件指令二级页面文本。"""
    lines = [
        f"{display_name(metadata)} · 插件帮助",
        f"名称：{metadata.name or '未知'}",
        f"版本：{version_text(metadata.version)}　作者：{metadata.author or '?'}",
        f"状态：{OK_MARK + ' 已启用' if metadata.activated else FAIL_MARK + ' 已禁用'}"
        f"　类型：{'内置' if metadata.reserved else '第三方'}",
    ]

    description = first_line(metadata.desc)
    if description:
        if len(description) > DESC_MAX_LEN:
            description = description[:DESC_MAX_LEN] + "…"
        lines.append(f"简介：{description}")

    if not metadata.activated:
        lines.extend(["", "提示：该插件已禁用，以下指令当前不会生效。"])

    lines.append("")
    if commands_error:
        lines.append(f"读取指令列表失败：{commands_error}")
    elif not commands:
        lines.append("该插件未注册任何指令（可能只提供事件监听或 LLM 工具）")
    else:
        disabled = sum(1 for item in commands if not item.get("enabled", True))
        head = f"指令（{len(commands)} 个"
        head += f"，其中 {disabled} 个已禁用）" if disabled else "）"
        lines.append(head + "：")
        for index, command in enumerate(commands, 1):
            lines.append(format_command(command, index))

    lines.extend(["", f"发送 {wake_prefix}插件列表 返回完整清单"])
    return "\n".join(lines)


def build_candidates_text(
    query: str, candidates: list[Any], wake_prefix: str
) -> str:
    """关键字未命中或存在歧义时的提示文本。"""
    if not candidates:
        return (
            f"未找到插件“{query}”。\n\n"
            f"发送 {wake_prefix}插件列表 查看已安装的插件清单。"
        )

    lines = [f"“{query}”匹配到多个插件，请写得更精确一些：", ""]
    for metadata in candidates[:MAX_CANDIDATES]:
        lines.append(f"- {display_name(metadata)}（{metadata.name or '未知'}）")
    if len(candidates) > MAX_CANDIDATES:
        lines.append(f"...另有 {len(candidates) - MAX_CANDIDATES} 个匹配项")
    example = display_name(candidates[0])
    lines.extend(
        ["", f"例如：{wake_prefix}查看{example}插件帮助"]
    )
    return "\n".join(lines)


async def collect_commands() -> tuple[dict[str, list[dict[str, Any]]], str | None]:
    """收集指令并按插件名分组，失败时返回错误信息。"""
    try:
        from astrbot.core.star import command_management

        rows = await command_management.list_commands()
    except Exception as error:
        return {}, f"{type(error).__name__}: {error}"

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row.get("plugin") or "", []).append(row)
    return grouped, None


class HelpPhraseFilter(CustomFilter):
    """匹配 “查看xx插件帮助” 句式，且要求消息已被唤醒（wake_prefix 或 @）。"""

    def filter(self, event: AstrMessageEvent, cfg: Any) -> bool:
        if not event.is_at_or_wake_command:
            return False
        return parse_help_phrase(event.get_message_str()) is not None


class PluginListPlugin(star.Star):
    """插件清单插件。"""

    def __init__(self, context: star.Context, config: Any = None) -> None:
        super().__init__(context)
        self.config = config or {}

    def _wake_prefix(self, event: AstrMessageEvent) -> str:
        try:
            config = self.context.get_config(event.unified_msg_origin)
            prefixes = config.get("wake_prefix", ["/"])
            if prefixes:
                return str(prefixes[0])
        except Exception as error:
            self.logger.debug("读取 wake_prefix 失败：%s", error)
        return "/"

    async def _reply(self, event: AstrMessageEvent, text: str) -> None:
        """优先发送渲染好的图片；渲染失败时退回纯文本。"""
        result = MessageEventResult()
        image = await render_image(text, self.config)
        if image is None:
            # 自己渲染失败，交给核心的文本转图片逻辑兜底（长文本才会渲染）。
            result.message(text).use_t2i(True)
        else:
            result.chain.append(image)
            result.use_t2i(False)
        event.set_result(result)
        event.stop_event()

    @filter.command("插件列表", alias={"插件清单"})
    async def plugin_list(self, event: AstrMessageEvent) -> None:
        """查看已安装插件清单（版本 / 启用状态 / 指令数）"""
        commands_by_plugin, commands_error = await collect_commands()
        text = build_list_text(
            list(star_registry),
            commands_by_plugin,
            self._wake_prefix(event),
            commands_error,
        )
        await self._reply(event, text)

    @filter.custom_filter(HelpPhraseFilter)
    async def plugin_help(self, event: AstrMessageEvent) -> None:
        """查看指定插件的指令（查看xx插件帮助）"""
        wake_prefix = self._wake_prefix(event)
        query = parse_help_phrase(event.get_message_str())
        if query is None:
            return

        metadata, candidates = match_plugin(query, list(star_registry))
        if metadata is None:
            await self._reply(
                event, build_candidates_text(query, candidates, wake_prefix)
            )
            return

        commands_by_plugin, commands_error = await collect_commands()
        text = build_detail_text(
            metadata,
            commands_by_plugin.get(metadata.name or "", []),
            wake_prefix,
            commands_error,
        )
        await self._reply(event, text)
