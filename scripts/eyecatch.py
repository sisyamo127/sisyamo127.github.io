"""アイキャッチ画像の作成(背景写真の左側に白いパネルを重ね、記事タイトルを載せる)。

サイズは1200x630(SNSでシェアされたときのOGP推奨サイズ)。
【】で始まるタイトルは、【】の中身をオレンジのラベル、残りを本文として表示する。
"""

import io
import os
import re

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 1200, 630
ACCENT = (255, 102, 0)  # サイトの購入ボタンと同じオレンジ
TEXT_COLOR = (40, 40, 40)
PANEL_WIDTH = 560  # 文字を置く左側パネルの幅
PANEL_FADE = 260  # パネルが写真に溶け込むまでのグラデーション幅
MARGIN_LEFT = 60

# Windows標準の日本語フォント(太字)を優先順に探す
FONT_CANDIDATES = [
    r"C:\Windows\Fonts\YuGothB.ttc",
    r"C:\Windows\Fonts\meiryob.ttc",
    r"C:\Windows\Fonts\BIZ-UDGothicB.ttc",
    r"C:\Windows\Fonts\NotoSansJP-VF.ttf",
]


def _font_path() -> str:
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            return path
    raise RuntimeError("日本語フォントが見つかりません(游ゴシック/メイリオ等)")


def split_title(title: str) -> tuple[str, str]:
    """「【福岡空港で買える!】子ども向け…」→ ("福岡空港で買える!", "子ども向け…")"""
    m = re.match(r"^【([^】]+)】\s*(.*)$", title.strip())
    return (m.group(1), m.group(2)) if m else ("", title.strip())


def _cover(img: Image.Image) -> Image.Image:
    """縦横比を保ったまま1200x630に切り抜く。"""
    scale = max(WIDTH / img.width, HEIGHT / img.height)
    img = img.resize((int(img.width * scale) + 1, int(img.height * scale) + 1), Image.LANCZOS)
    left, top = (img.width - WIDTH) // 2, (img.height - HEIGHT) // 2
    return img.crop((left, top, left + WIDTH, top + HEIGHT)).convert("RGBA")


def _wrap_by_width(draw, text, font, max_width) -> list:
    """1文字ずつ幅に収まるよう折り返す(改行位置をAIで決められなかったときの予備)。"""
    lines, line = [], ""
    for ch in text:
        if draw.textlength(line + ch, font=font) <= max_width or not line:
            line += ch
        elif ch in "、。・!！?？」)":
            line += ch
        else:
            lines.append(line)
            line = ch
    if line:
        lines.append(line)
    return lines


def _layout_lines(draw, text, max_width, max_lines, start_size, min_size, line_splitter):
    font_path = _font_path()
    font_for = lambda size: ImageFont.truetype(font_path, size)  # noqa: E731

    lines = None
    if line_splitter:
        try:
            lines = line_splitter(text, max(4, int(max_width / start_size)), max_lines)
        except Exception:
            lines = None
    if lines:
        size = start_size
        while size > min_size and max(draw.textlength(l, font=font_for(size)) for l in lines) > max_width:
            size -= 2
        return font_for(size), lines

    size = start_size
    while size > min_size:
        lines = _wrap_by_width(draw, text, font_for(size), max_width)
        if len(lines) <= max_lines:
            return font_for(size), lines
        size -= 4
    return font_for(min_size), _wrap_by_width(draw, text, font_for(min_size), max_width)


def render_eyecatch(photo_bytes: bytes, title: str, line_splitter=None) -> bytes:
    """背景写真とタイトルからアイキャッチ画像(JPEG)を作る。

    line_splitter(text, max_chars, max_lines) -> list[str] を渡すと、単語の途中で
    改行しないよう改行位置をその関数に決めさせる(結合して元の文字列と一致しない場合は使わない)。
    """
    img = _cover(Image.open(io.BytesIO(photo_bytes)))

    panel = Image.new("RGBA", img.size)
    panel_draw = ImageDraw.Draw(panel)
    for x in range(WIDTH):
        if x < PANEL_WIDTH:
            alpha = 245
        else:
            alpha = max(0, int(245 * (1 - (x - PANEL_WIDTH) / PANEL_FADE)))
        panel_draw.line([(x, 0), (x, HEIGHT)], fill=(255, 255, 255, alpha))
    img = Image.alpha_composite(img, panel)
    draw = ImageDraw.Draw(img)

    label_text, main_text = split_title(title)

    def checked_splitter(text, max_chars, max_lines):
        lines = line_splitter(text, max_chars, max_lines) if line_splitter else None
        return lines if lines and "".join(lines) == text and len(lines) <= max_lines else None

    font, lines = _layout_lines(
        draw, main_text, PANEL_WIDTH - MARGIN_LEFT, 3, 72, 40, checked_splitter if line_splitter else None
    )
    line_height = font.size + 16

    label_height = 0
    label_font = None
    if label_text:
        label_font = ImageFont.truetype(_font_path(), 38)
        label_height = 38 + 20 + 20  # 文字 + 上下余白 + ラベル下の間隔

    total = label_height + len(lines) * line_height
    y = (HEIGHT - total) // 2

    if label_text:
        tw = draw.textlength(label_text, font=label_font)
        draw.rounded_rectangle((MARGIN_LEFT, y, MARGIN_LEFT + tw + 44, y + 38 + 20), radius=10, fill=ACCENT)
        draw.text((MARGIN_LEFT + 22, y + 6), label_text, font=label_font, fill=(255, 255, 255))
        y += label_height

    for line in lines:
        draw.text((MARGIN_LEFT, y), line, font=font, fill=TEXT_COLOR)
        y += line_height
    draw.rectangle((MARGIN_LEFT, y + 6, MARGIN_LEFT + 140, y + 14), fill=ACCENT)

    out = io.BytesIO()
    img.convert("RGB").save(out, format="JPEG", quality=90)
    return out.getvalue()
