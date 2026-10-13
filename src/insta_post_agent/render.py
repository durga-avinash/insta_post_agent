from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .models import Story

WIDTH, HEIGHT = 1080, 1350
CREAM = "#F5F0E6"
INK = "#17232D"
MUTED = "#52616B"
BLUE = "#226E8A"
CORAL = "#E37758"
GOLD = "#F0BC52"
GREEN = "#4E9078"
PAPER = "#FFFFFF"


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        ["C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/segoeuib.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
        if bold
        else ["C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/segoeui.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
    )
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        # RSS titles occasionally contain an unbroken token. Split those safely
        # instead of allowing a single line to run beyond the canvas.
        if draw.textlength(word, font=font) > max_width:
            if current:
                lines.append(current)
                current = ""
            remainder = word
            while draw.textlength(remainder, font=font) > max_width:
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
    return lines or [""]


def _ellipsis(draw: ImageDraw.ImageDraw, line: str, font: ImageFont.ImageFont, max_width: int) -> str:
    result = line.rstrip()
    while result and draw.textlength(result + "…", font=font) > max_width:
        result = result[:-1].rstrip()
    return result + "…"


def _draw_illustration(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], category: str, index: int) -> None:
    x0, y0, x1, y1 = box
    draw.rounded_rectangle(box, radius=34, fill="#E7E2D6")
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    accent = [BLUE, CORAL, GREEN, "#7C6FA5", GOLD, "#4A7DA6"][index % 6]
    draw.ellipse((cx - 175, cy - 150, cx + 175, cy + 200), fill="#D3E2E2")
    draw.ellipse((cx - 123, cy - 123, cx + 123, cy + 123), outline=accent, width=12)
    label = category.upper()
    if "ECONOMY" in label:
        points = [(cx - 90, cy + 65), (cx - 35, cy + 20), (cx + 15, cy + 38), (cx + 83, cy - 55)]
        draw.line(points, fill=accent, width=16, joint="curve")
        for px, py in points:
            draw.ellipse((px - 12, py - 12, px + 12, py + 12), fill=CORAL)
        draw.rectangle((cx - 120, cy + 87, cx + 120, cy + 104), fill=INK)
    elif "CLIMATE" in label:
        draw.ellipse((cx - 55, cy - 90, cx + 55, cy + 20), fill=GOLD)
        for offset in (-120, -40, 40, 120):
            draw.line((cx + offset, cy - 115, cx + offset - 22, cy - 155), fill=CORAL, width=8)
        draw.arc((cx - 140, cy - 20, cx + 140, cy + 180), start=180, end=360, fill=GREEN, width=18)
        draw.line((cx - 135, cy + 80, cx + 135, cy + 80), fill=GREEN, width=14)
    elif "SCIENCE" in label:
        draw.ellipse((cx - 26, cy - 26, cx + 26, cy + 26), fill=GOLD)
        draw.ellipse((cx - 135, cy - 50, cx + 135, cy + 50), outline=accent, width=9)
        draw.ellipse((cx - 55, cy - 125, cx + 55, cy + 125), outline=CORAL, width=9)
        draw.polygon([(cx + 100, cy - 85), (cx + 145, cy - 100), (cx + 132, cy - 52)], fill=INK)
    elif "COURT" in label or "POLITICS" in label:
        roof = [(cx - 138, cy - 82), (cx, cy - 154), (cx + 138, cy - 82)]
        draw.polygon(roof, fill=GOLD)
        draw.rectangle((cx - 130, cy - 73, cx + 130, cy - 51), fill=INK)
        for offset in (-88, -30, 30, 88):
            draw.rounded_rectangle((cx + offset - 10, cy - 48, cx + offset + 10, cy + 95), radius=8, fill=accent)
        draw.rectangle((cx - 151, cy + 100, cx + 151, cy + 123), fill=INK)
    elif "CONFLICT" in label:
        draw.polygon([(cx - 105, cy + 65), (cx - 35, cy - 25), (cx + 36, cy + 65)], fill=CORAL)
        draw.polygon([(cx - 10, cy + 65), (cx + 72, cy - 88), (cx + 146, cy + 65)], fill=GOLD)
        draw.line((cx - 140, cy + 103, cx + 150, cy + 103), fill=INK, width=13)
    else:
        # An editorial information motif instead of an invented photo of a real event.
        draw.rounded_rectangle((cx - 122, cy - 112, cx + 122, cy + 116), radius=24, fill=accent)
        draw.ellipse((cx - 44, cy - 58, cx + 44, cy + 30), fill=GOLD)
        draw.arc((cx - 78, cy + 2, cx + 78, cy + 152), start=180, end=360, fill=PAPER, width=18)
        draw.line((cx - 100, cy + 88, cx + 100, cy + 88), fill=INK, width=10)
    for i in range(5):
        px, py = x0 + 75 + i * 43, y1 - 62
        draw.ellipse((px, py, px + 10, py + 10), fill=accent if i % 2 == 0 else "#A7B8B8")


def render_slide(story: Story, slide_number: int, total: int, output_path: Path) -> dict[str, object]:
    image = Image.new("RGB", (WIDTH, HEIGHT), CREAM)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, WIDTH, 20), fill=BLUE)
    region_accent = BLUE if story.region == "INDIA" else CORAL
    draw.rounded_rectangle((74, 62, 264, 120), radius=24, fill=region_accent)
    draw.text((97, 76), story.region, font=_font(26, True), fill=PAPER)
    draw.text((286, 77), "THE DAILY BRIEF", font=_font(25, True), fill=INK)
    draw.text((850, 80), f"{slide_number:02d} / {total:02d}", font=_font(24, True), fill=MUTED)
    draw.rounded_rectangle((74, 157, 390, 202), radius=19, fill="#E4D9C4")
    draw.text((91, 166), story.category.upper()[:28], font=_font(19, True), fill=INK)
    if story.trend_status == "CONFIRMED_GOOGLE_TRENDS" and story.trend_evidence:
        evidence = story.trend_evidence[0]
        trend_text = f"TRENDING {evidence['geo']} · {evidence['approx_traffic']}"
        draw.rounded_rectangle((408, 157, 650, 202), radius=19, fill="#D8E9DD")
        draw.text((422, 167), trend_text[:24], font=_font(15, True), fill="#225D43")
    else:
        draw.rounded_rectangle((408, 157, 650, 202), radius=19, fill="#F0D9A5")
        draw.text((422, 167), "TREND UNCONFIRMED", font=_font(15, True), fill="#6B4D0C")
    if story.verification_status == "NEEDS_REVIEW":
        draw.rounded_rectangle((666, 157, 1006, 202), radius=19, fill="#A84A3F")
        draw.text((681, 168), "NEEDS HUMAN REVIEW", font=_font(16, True), fill=PAPER)

    title_font_size = 56
    title_font = _font(title_font_size, True)
    lines = _wrap(draw, story.headline, title_font, 925)
    while (len(lines) > 5 or len(lines) * (title_font_size + 12) > 250) and title_font_size > 32:
        title_font_size = max(32, title_font_size - 2)
        title_font = _font(title_font_size, True)
        lines = _wrap(draw, story.headline, title_font, 925)
    headline_truncated = len(lines) > 5
    if headline_truncated:
        lines = lines[:5]
        lines[-1] = _ellipsis(draw, lines[-1], title_font, 925)
    title_y = 230
    line_height = title_font_size + 12
    title_overflow = False
    for line in lines:
        bbox = draw.textbbox((74, title_y), line, font=title_font)
        title_overflow = title_overflow or bbox[2] > 999 or bbox[3] > 482
        draw.text((74, title_y), line, font=title_font, fill=INK)
        title_y += line_height

    illustration_top = max(495, title_y + 18)
    illustration_bottom = 1010
    _draw_illustration(draw, (74, illustration_top, 1006, illustration_bottom), story.category, slide_number)
    draw.text((104, illustration_top + 25), "ILLUSTRATION", font=_font(17, True), fill=MUTED)

    summary_font_size = 26
    summary_font = _font(summary_font_size)
    summary_lines = _wrap(draw, story.summary, summary_font, 910)
    while (len(summary_lines) > 4 or len(summary_lines) * (summary_font_size + 7) > 120) and summary_font_size > 21:
        summary_font_size -= 1
        summary_font = _font(summary_font_size)
        summary_lines = _wrap(draw, story.summary, summary_font, 910)
    summary_truncated = len(summary_lines) > 4
    if summary_truncated:
        summary_lines = summary_lines[:4]
        summary_lines[-1] = _ellipsis(draw, summary_lines[-1], summary_font, 910)
    summary_y = 1045
    summary_overflow = False
    for line in summary_lines:
        bbox = draw.textbbox((74, summary_y), line, font=summary_font)
        summary_overflow = summary_overflow or bbox[2] > 984 or bbox[3] > 1175
        draw.text((74, summary_y), line, font=summary_font, fill=INK)
        summary_y += summary_font_size + 7
    source_date = story.articles[0].published_at.date().isoformat() if story.articles and story.articles[0].published_at else "date unavailable"
    footer_y = max(1185, summary_y + 16)
    draw.line((74, footer_y, 1006, footer_y), fill="#C9C2B4", width=2)
    draw.text((74, footer_y + 23), f"SOURCE  {story.publisher[:52]}", font=_font(19, True), fill=MUTED)
    draw.text((74, footer_y + 57), f"Published: {source_date}  •  Review status: {story.verification_status}", font=_font(17), fill=MUTED)
    if story.trend_status == "CONFIRMED_GOOGLE_TRENDS" and story.trend_evidence:
        evidence = story.trend_evidence[0]
        trend_line = f"Google Trends {evidence['geo']}: {evidence['query'][:54]} · {evidence['approx_traffic']} approx."
        draw.text((74, footer_y + 82), trend_line, font=_font(15), fill=MUTED)
    else:
        draw.text((74, footer_y + 82), "Trend status: not confirmed by Google Trends", font=_font(15), fill=MUTED)
    draw.text((74, 1295), "Illustrative editorial graphic • See caption for source link", font=_font(16), fill=MUTED)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path, format="PNG", optimize=True)
    with Image.open(output_path) as check:
        if check.size != (WIDTH, HEIGHT):
            raise ValueError(f"Unexpected rendered size: {check.size}")
    return {
        "path": str(output_path),
        "width": WIDTH,
        "height": HEIGHT,
        "text_overflow": title_overflow or summary_overflow,
        "headline_truncated": headline_truncated,
        "summary_truncated": summary_truncated,
        "review_badge": story.verification_status == "NEEDS_REVIEW",
    }
