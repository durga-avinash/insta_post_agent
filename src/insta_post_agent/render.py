from __future__ import annotations

import logging
import re
from datetime import timedelta, timezone
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .models import Story
from .verification import deterministic_key_takeaways

LOGGER = logging.getLogger(__name__)
WIDTH, HEIGHT = 1080, 1350
CREAM = "#F8F5EE"
NAVY = "#062143"
GOLD = "#FFC332"
BLUE_PALE = "#DFEDF2"
TEXT = "#092649"
MUTED = "#4C607A"
RULE = "#173456"
PANEL = "#F0F1F1"

_NUMBER_RE = re.compile(r"(?:₹|\$|€|£|\bINR\s*|\bUSD\s*)?\b\d[\d,.]*(?:\s?(?:%|bn|billion|mn|million|crore|cr|lakh|lakhs|million|billion|trillion|basis points?|bps))\b", re.I)
_IST = timezone(timedelta(hours=5, minutes=30), name="IST")


@lru_cache(maxsize=128)
def _font(size: int, bold: bool = False, condensed: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if bold:
        candidates = [
            "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/seguisb.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ]
    else:
        candidates = [
            "C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/segoeui.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
    if condensed:
        candidates = [p for p in candidates if "Condensed" in p] + candidates
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
    words = (text or "").split()
    if not words:
        return [""]
    lines: list[str] = []
    current = ""
    for word in words:
        if draw.textlength(word, font=font) > max_width:
            if current:
                lines.append(current)
                current = ""
            remainder = word
            while remainder and draw.textlength(remainder, font=font) > max_width:
                low, high = 1, len(remainder)
                while low < high:
                    middle = (low + high + 1) // 2
                    if draw.textlength(remainder[:middle], font=font) <= max_width:
                        low = middle
                    else:
                        high = middle - 1
                lines.append(remainder[:low])
                remainder = remainder[low:]
            current = remainder
            continue
        candidate = f"{current} {word}".strip()
        if current and draw.textlength(candidate, font=font) > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _ellipsis(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> str:
    value = text.rstrip()
    while value and draw.textlength(value + "…", font=font) > max_width:
        value = value[:-1].rstrip()
    return (value + "…") if value else "…"


def _fit_lines(
    draw: ImageDraw.ImageDraw,
    text: str,
    *,
    max_width: int,
    max_lines: int,
    max_size: int,
    min_size: int,
    bold: bool = False,
) -> tuple[ImageFont.ImageFont, list[str], bool]:
    size = max_size
    font = _font(size, bold=bold)
    lines = _wrap(draw, text, font, max_width)
    while (len(lines) > max_lines or len(lines) * (size + 9) > 232) and size > min_size:
        size -= 2
        font = _font(size, bold=bold)
        lines = _wrap(draw, text, font, max_width)
    truncated = len(lines) > max_lines
    if truncated:
        lines = lines[:max_lines]
        lines[-1] = _ellipsis(draw, lines[-1], font, max_width)
    return font, lines, truncated


def _draw_corner_pattern(draw: ImageDraw.ImageDraw) -> None:
    # The sample's navy/gold corner cuts and dotted margins, rebuilt as vectors.
    draw.polygon([(0, 0), (82, 0), (0, 100)], fill=GOLD)
    draw.polygon([(48, 0), (90, 0), (0, 111), (0, 70)], fill=NAVY)
    draw.polygon([(0, 836), (46, 892), (46, 1044), (0, 1012)], fill=GOLD)
    draw.polygon([(1080, 388), (1080, 493), (1038, 534), (1038, 431)], fill=GOLD)
    draw.polygon([(1080, 485), (1080, 539), (1050, 568), (1050, 514)], fill=NAVY)
    for row in range(6):
        for col in range(4):
            x = 12 + col * 18
            y = 354 + row * 18
            draw.ellipse((x, y, x + 4, y + 4), fill="#294465")
            x2 = 1050 + col * 9
            y2 = 286 + row * 18
            if x2 < WIDTH - 2:
                draw.ellipse((x2, y2, x2 + 4, y2 + 4), fill="#294465")
    # Quiet hatch marks sit outside the copy area as in the reference paper edge.
    for offset in range(-120, 120, 24):
        draw.line((1042 + offset, 1170, 1080 + offset, 1132), fill="#EAE5DC", width=4)


def _place_logo(canvas: Image.Image, path: Path, box: tuple[int, int, int, int]) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"Required editorial logo asset is missing: {path}")
    left, top, right, bottom = box
    logo = _logo_at_size(str(path.resolve()), right - left, bottom - top)
    x = left + (right - left - logo.width) // 2
    y = top + (bottom - top - logo.height) // 2
    canvas.paste(logo, (x, y), logo if logo.getchannel("A").getextrema() != (255, 255) else None)


@lru_cache(maxsize=8)
def _logo_at_size(path: str, max_width: int, max_height: int) -> Image.Image:
    with Image.open(path) as source:
        logo = source.convert("RGBA")
    logo.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)
    return logo


def _draw_brand_header(draw: ImageDraw.ImageDraw, slide_number: int, total: int, story: Story) -> None:
    # Editable title lockup: the logos are separate original image assets.
    draw.polygon([(419, 40), (875, 15), (871, 108), (420, 124)], fill=GOLD)
    daily_font = _font(64, bold=True, condensed=True)
    draw.text((486, 38), "DAILY", font=daily_font, fill=NAVY, stroke_width=1, stroke_fill=NAVY)
    draw.polygon([(294, 134), (891, 101), (899, 221), (284, 257)], fill=NAVY)
    news_font = _font(70, bold=True, condensed=True)
    nuggets_font = _font(67, bold=True, condensed=True)
    news_x = 310
    draw.text((news_x, 147), "NEWS", font=news_font, fill="#FFFFFF")
    nuggets_x = news_x + int(draw.textlength("NEWS", font=news_font)) + 22
    draw.text((nuggets_x, 139), "NUGGETS", font=nuggets_font, fill=GOLD)
    # Fixed editorial rays; no story-specific illustration is ever drawn.
    for x, y, dx, dy in ((350, 129, -22, -40), (386, 120, -8, -48), (730, 120, 18, -45), (767, 126, 33, -36)):
        draw.line((x, y, x + dx, y + dy), fill=NAVY, width=10)
    tagline = "KEY STORIES  |  BIG PICTURE  |  REAL IMPACT"
    draw.text((338, 277), tagline, font=_font(22, bold=True), fill=NAVY)
    draw.line((52, 354, 1028, 354), fill=NAVY, width=2)
    draw.rounded_rectangle((55, 378, 802, 437), radius=28, fill=BLUE_PALE)
    draw.ellipse((62, 383, 108, 429), fill=GOLD)
    draw.ellipse((75, 396, 95, 416), fill=NAVY)
    category = story.category.upper().strip() or "CATEGORY UNAVAILABLE"
    region_category = f"{story.region}  /  {category}"
    label_font = _fit_lines(draw, region_category, max_width=465, max_lines=1, max_size=22, min_size=14, bold=True)[0]
    draw.text((123, 395), region_category, font=label_font, fill=NAVY)
    if story.selection_basis == "EDITORIAL_BACKUP":
        trend_label = "EDITOR BACKUP"
        trend_fill, trend_ink = "#F5E4B4", "#765410"
    elif story.selection_basis == "CROSS_PUBLISHER_COVERAGE":
        count = next((item.get("independent_publisher_count") for item in story.trend_evidence if item.get("signal_type") == "independent_publisher_coverage"), None)
        trend_label = f"COVERAGE  {count}+ SOURCES" if count else "WIDE COVERAGE"
        trend_fill, trend_ink = "#D6E8D6", "#234E35"
    elif story.trend_status == "CONFIRMED_GOOGLE_TRENDS" and story.trend_evidence:
        traffic = str(story.trend_evidence[0].get("approx_traffic") or "TREND MATCHED")
        trend_label = f"TRENDS  {traffic}"
        trend_fill, trend_ink = "#D6E8D6", "#234E35"
    else:
        trend_label = "TREND UNCONFIRMED"
        trend_fill, trend_ink = "#F5E4B4", "#765410"
    draw.rounded_rectangle((606, 387, 792, 428), radius=18, fill=trend_fill)
    trend_font = _fit_lines(draw, trend_label, max_width=165, max_lines=1, max_size=16, min_size=12, bold=True)[0]
    trend_width = draw.textlength(trend_label, font=trend_font)
    draw.text((699 - trend_width / 2, 398), trend_label, font=trend_font, fill=trend_ink)
    draw.polygon([(832, 359), (1080, 359), (1080, 459), (807, 459)], fill=NAVY)
    number = f"{slide_number:02d}"
    slash = f"/ {total:02d}"
    draw.text((866, 372), number, font=_font(58, bold=True), fill=GOLD)
    draw.text((957, 386), slash, font=_font(42, bold=True), fill="#FFFFFF")


def _draw_section_label(draw: ImageDraw.ImageDraw, label: str, y: int, status: str = "") -> None:
    draw.text((62, y), label, font=_font(22, bold=True), fill=NAVY, stroke_width=0)
    draw.line((229, y + 18, 1019, y + 18), fill=NAVY, width=2)
    if status:
        status_font = _font(16, bold=True)
        width = draw.textlength(status, font=status_font)
        x = max(720, int(1014 - width))
        if status in {"NEEDS HUMAN REVIEW", "TREND UNCONFIRMED"}:
            fill = "#9A3E35"
            draw.rounded_rectangle((x - 10, y - 2, 1018, y + 31), radius=12, fill=fill)
            draw.text((x, y + 4), status, font=status_font, fill="#FFFFFF")
        else:
            draw.text((x, y + 3), status, font=status_font, fill=MUTED)


def _published_label(story: Story) -> str:
    if story.event_date:
        return story.event_date.strip()
    published = next((article.published_at for article in story.articles if article.published_at), None)
    if published is None:
        return "DATE UNAVAILABLE"
    return published.astimezone(_IST).strftime("%d %b %Y").upper()


def _stat_facts(story: Story, limit: int = 3) -> list[str]:
    found: list[str] = []
    for article in story.articles:
        for sentence in re.split(r"(?<=[.!?])\s+", article.description):
            clean = " ".join(sentence.split()).strip()
            if len(clean) < 25 or not _NUMBER_RE.search(clean):
                continue
            if clean not in found:
                found.append(clean)
            if len(found) == limit:
                return found
    return found


def _layout_type(story: Story, slide_number: int) -> str:
    if _stat_facts(story):
        return "statistics"
    if slide_number == 1:
        return "lead"
    return "brief"


def _draw_headline(draw: ImageDraw.ImageDraw, story: Story, layout_type: str) -> tuple[int, bool, bool]:
    status = "NEEDS HUMAN REVIEW" if story.verification_status == "NEEDS_REVIEW" else (
        "TREND UNCONFIRMED" if story.trend_status not in {"CONFIRMED_GOOGLE_TRENDS", "CONFIRMED_CROSS_PUBLISHER_COVERAGE"} else ""
    )
    _draw_section_label(draw, "HEADLINE", 475, status)
    sizes = {"lead": (84, 48), "brief": (72, 46), "statistics": (76, 46)}
    max_size, min_size = sizes[layout_type]
    font, lines, truncated = _fit_lines(
        draw, story.headline or "Headline unavailable — open source link",
        max_width=948, max_lines=3, max_size=max_size, min_size=min_size, bold=True,
    )
    y = 520
    line_height = font.size + 8 if hasattr(font, "size") else 56
    overflow = False
    for line in lines:
        bbox = draw.textbbox((61, y), line, font=font)
        overflow |= bbox[2] > 1019 or bbox[3] > 752
        draw.text((61, y), line, font=font, fill=NAVY)
        y += line_height
    if lines:
        highlight_y = min(752, y - 3)
        width = min(650, int(draw.textlength(lines[-1], font=font)))
        draw.rectangle((62, highlight_y, 62 + width, highlight_y + 9), fill=GOLD)
    return min(760, y + 2), truncated, overflow


def _draw_summary(draw: ImageDraw.ImageDraw, story: Story, layout_type: str) -> tuple[bool, bool]:
    summary_y = 777 if layout_type != "statistics" else 777
    _draw_section_label(draw, "SUMMARY", summary_y)
    summary = (story.summary or "Summary unavailable. Open the original source before using this story.").strip()
    font, lines, truncated = _fit_lines(
        draw, summary, max_width=955, max_lines=3, max_size=31, min_size=23, bold=False,
    )
    y = 818
    line_height = font.size + 7 if hasattr(font, "size") else 30
    overflow = False
    for line in lines:
        bbox = draw.textbbox((62, y), line, font=font)
        overflow |= bbox[2] > 1018 or bbox[3] > 949
        draw.text((62, y), line, font=font, fill=TEXT)
        y += line_height
    return truncated, overflow


def _draw_takeaways_panel(draw: ImageDraw.ImageDraw, story: Story, layout_type: str) -> tuple[bool, bool]:
    top, bottom = 964, 1184
    draw.rounded_rectangle((56, top, 1024, bottom), radius=30, fill=PANEL)
    draw.polygon([(56, top + 28), (56, top + 29), (300, top), (330, top), (263, bottom), (56, bottom)], fill=NAVY)
    draw.polygon([(300, top), (330, top), (263, bottom), (235, bottom)], fill=GOLD)
    if layout_type == "statistics":
        heading = ["KEY", "FIGURES"]
        lines = _stat_facts(story)
    else:
        heading = ["KEY", "TAKEAWAYS"]
        lines = deterministic_key_takeaways(story.articles, limit=3)
    draw.text((86, top + 47), heading[0], font=_font(26, bold=True), fill="#FFFFFF")
    draw.text((77, top + 81), heading[1], font=_font(24, bold=True), fill="#FFFFFF")
    if not lines:
        lines = ["No additional source detail was included in the available feed text."]
    overflow = False
    truncated_any = False
    row_top = top + 13
    row_height = 62
    for index, text in enumerate(lines[:3]):
        row_y = row_top + index * row_height
        if index:
            draw.line((454, row_y, 990, row_y), fill="#D3D7D9", width=1)
        draw.ellipse((356, row_y + 7, 409, row_y + 60), fill=GOLD)
        draw.text((365, row_y + 16), f"{index + 1:02d}", font=_font(22, bold=True), fill=NAVY)
        font, wrapped, truncated = _fit_lines(
            draw, text, max_width=540, max_lines=2, max_size=23, min_size=16, bold=False,
        )
        truncated_any |= truncated
        y = row_y + (13 if len(wrapped) == 1 else 6)
        for line in wrapped:
            bbox = draw.textbbox((454, y), line, font=font)
            overflow |= bbox[2] > 1000 or bbox[3] > row_y + row_height
            draw.text((454, y), line, font=font, fill=TEXT)
            y += font.size + 2 if hasattr(font, "size") else 20
    return overflow, truncated_any


def _draw_metadata(draw: ImageDraw.ImageDraw, story: Story) -> None:
    top = 1200
    draw.line((56, top, 1024, top), fill=NAVY, width=2)
    draw.line((375, top + 12, 375, 1289), fill="#B4BEC6", width=2)
    draw.line((704, top + 12, 704, 1289), fill="#B4BEC6", width=2)
    # Static utility glyphs from the reference template, drawn from simple lines.
    # Calendar
    draw.rounded_rectangle((86, 1215, 126, 1256), radius=6, outline=NAVY, width=5)
    draw.line((86, 1228, 126, 1228), fill=NAVY, width=4)
    draw.line((97, 1209, 97, 1221), fill=NAVY, width=5)
    draw.line((115, 1209, 115, 1221), fill=NAVY, width=5)
    date_font = _font(23, bold=True)
    draw.text((145, 1215), _published_label(story), font=date_font, fill=NAVY)
    date_label = "VERIFIED EVENT DATE" if story.event_date else "PUBLISH DATE"
    draw.text((145, 1247), date_label, font=_font(16), fill=MUTED)
    # Newspaper
    draw.rectangle((410, 1214, 450, 1257), outline=NAVY, width=4)
    draw.rectangle((417, 1222, 429, 1235), outline=NAVY, width=3)
    for y in (1222, 1231, 1240, 1249):
        draw.line((434, y, 444, y), fill=NAVY, width=3)
    source_text = story.publisher or "Source attribution unavailable"
    source_text = source_text.split(",")[:3]
    source_label = ", ".join(item.strip() for item in source_text if item.strip())
    source_font = _font(22, bold=True)
    while draw.textlength(source_label, font=source_font) > 235 and source_font.size > 16:
        source_font = _font(source_font.size - 1, bold=True)
    draw.text((467, 1215), source_label, font=source_font, fill=NAVY)
    draw.text((467, 1247), "SOURCE PUBLISHERS", font=_font(16), fill=MUTED)
    # Link reminder (actual URLs remain in caption and workbook)
    draw.ellipse((744, 1214, 786, 1256), outline=NAVY, width=4)
    draw.line((754, 1246, 778, 1222), fill=NAVY, width=5)
    draw.text((800, 1215), "Original links in caption", font=_font(21, bold=True), fill=NAVY)
    draw.text((800, 1247), "OPEN EACH SOURCE BEFORE USE", font=_font(15), fill=MUTED)


def _draw_footer(draw: ImageDraw.ImageDraw) -> None:
    draw.rectangle((0, 1300, WIDTH, HEIGHT), fill=NAVY)
    draw.text((54, 1320), "IDEAS   |   PEOPLE   |   OPPORTUNITIES", font=_font(17), fill="#FFFFFF")
    draw.line((548, 1335, 789, 1335), fill="#D5DFE7", width=2)
    draw.line((833, 1344, 854, 1324, 870, 1333, 891, 1309), fill=GOLD, width=7, joint="curve")
    draw.polygon([(891, 1309), (889, 1332), (877, 1321)], fill=GOLD)
    draw.text((915, 1313), "STAY INFORMED", font=_font(15, bold=True), fill=GOLD)
    draw.text((915, 1334), "STAY AHEAD", font=_font(15, bold=True), fill=GOLD)


def render_slide(
    story: Story,
    slide_number: int,
    total: int,
    output_path: Path,
    *,
    assets_dir: Path | None = None,
) -> dict[str, object]:
    """Render a template-faithful, text-editable editorial slide at 4:5 size."""
    image = Image.new("RGB", (WIDTH, HEIGHT), CREAM)
    draw = ImageDraw.Draw(image)
    _draw_corner_pattern(draw)
    project_root = Path(__file__).resolve().parents[2]
    asset_root = assets_dir or project_root / "assets"
    _place_logo(image, asset_root / "dms_iitd_logo.png", (34, 58, 296, 198))
    _place_logo(image, asset_root / "ppc_logo.jpeg", (932, 28, 1054, 158))
    draw = ImageDraw.Draw(image)
    _draw_brand_header(draw, slide_number, total, story)
    layout_type = _layout_type(story, slide_number)
    title_bottom, headline_truncated, headline_overflow = _draw_headline(draw, story, layout_type)
    summary_truncated, summary_overflow = _draw_summary(draw, story, layout_type)
    takeaways_overflow, takeaways_truncated = _draw_takeaways_panel(draw, story, layout_type)
    _draw_metadata(draw, story)
    _draw_footer(draw)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path, format="PNG", optimize=False, compress_level=6)
    with Image.open(output_path) as check:
        if check.size != (WIDTH, HEIGHT):
            raise ValueError(f"Unexpected rendered size: {check.size}")
    text_overflow = headline_overflow or summary_overflow or takeaways_overflow
    return {
        "path": str(output_path),
        "width": WIDTH,
        "height": HEIGHT,
        "text_overflow": text_overflow,
        "headline_truncated": headline_truncated,
        "summary_truncated": summary_truncated,
        "review_badge": story.verification_status == "NEEDS_REVIEW",
        "layout_type": layout_type,
        "logo_assets_validated": True,
        "takeaways_truncated": takeaways_truncated,
    }
