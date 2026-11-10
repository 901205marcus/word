from __future__ import annotations

import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps


class ImageTextComposer:
    def __init__(self, max_dimension: int = 2048):
        self.max_dimension = max_dimension

    def compose(self, image_path: str | Path, text: str, output_path: str | Path) -> str:
        caption = text.strip()
        if not caption:
            raise ValueError("沒有可放到照片上的文字。")

        source = Path(image_path)
        if not source.exists():
            raise FileNotFoundError(f"找不到照片：{source.name}")

        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)

        with Image.open(source) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGBA")

        image.thumbnail((self.max_dimension, self.max_dimension), Image.Resampling.LANCZOS)
        width, height = image.size
        if width < 120 or height < 120:
            raise ValueError("照片尺寸太小，無法清楚放上文字。")

        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        font, lines, spacing, text_bbox = self._fit_text(draw, caption, width, height)

        text_width = text_bbox[2] - text_bbox[0]
        text_height = text_bbox[3] - text_bbox[1]
        center_x = width / 2
        center_y = height / 2
        padding_x = max(20, int(font.size * 0.55))
        padding_y = max(16, int(font.size * 0.4))
        box = (
            int(center_x - text_width / 2 - padding_x),
            int(center_y - text_height / 2 - padding_y),
            int(center_x + text_width / 2 + padding_x),
            int(center_y + text_height / 2 + padding_y),
        )
        radius = max(16, int(font.size * 0.35))
        draw.rounded_rectangle(box, radius=radius, fill=(0, 0, 0, 120))

        stroke_width = max(2, font.size // 18)
        draw.multiline_text(
            (center_x, center_y),
            "\n".join(lines),
            font=font,
            fill=(255, 255, 255, 255),
            anchor="mm",
            align="center",
            spacing=spacing,
            stroke_width=stroke_width,
            stroke_fill=(0, 0, 0, 235),
        )

        result = Image.alpha_composite(image, overlay).convert("RGB")
        result.save(output, format="JPEG", quality=91, optimize=True, progressive=True)
        return str(output)

    def _fit_text(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        width: int,
        height: int,
    ) -> tuple[ImageFont.FreeTypeFont, list[str], int, tuple[int, int, int, int]]:
        max_width = int(width * 0.82)
        max_height = int(height * 0.68)
        maximum = max(28, min(int(min(width, height) * 0.22), 220))
        minimum = max(18, min(width, height) // 30)

        for font_size in range(maximum, minimum - 1, -4):
            font = self._load_font(font_size)
            lines = self._wrap_text(draw, text, font, max_width)
            spacing = max(6, font_size // 4)
            bbox = draw.multiline_textbbox(
                (0, 0),
                "\n".join(lines),
                font=font,
                align="center",
                spacing=spacing,
                stroke_width=max(2, font_size // 18),
            )
            if bbox[2] - bbox[0] <= max_width and bbox[3] - bbox[1] <= max_height:
                return font, lines, spacing, bbox

        font = self._load_font(minimum)
        lines = self._wrap_text(draw, text, font, max_width)
        spacing = max(5, minimum // 4)
        bbox = draw.multiline_textbbox(
            (0, 0),
            "\n".join(lines),
            font=font,
            align="center",
            spacing=spacing,
            stroke_width=max(2, minimum // 18),
        )
        if bbox[3] - bbox[1] > max_height:
            raise ValueError("文字太長，無法完整放入照片中央，請縮短文字後重試。")
        return font, lines, spacing, bbox

    def _wrap_text(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        font: ImageFont.FreeTypeFont,
        max_width: int,
    ) -> list[str]:
        lines: list[str] = []
        for paragraph in text.splitlines() or [text]:
            paragraph = paragraph.strip()
            if not paragraph:
                lines.append("")
                continue

            current = ""
            for character in paragraph:
                candidate = f"{current}{character}"
                bbox = draw.textbbox((0, 0), candidate, font=font)
                if current and bbox[2] - bbox[0] > max_width:
                    lines.append(current.rstrip())
                    current = character.lstrip()
                else:
                    current = candidate
            if current:
                lines.append(current.rstrip())
        return lines or [""]

    @staticmethod
    def _load_font(size: int) -> ImageFont.FreeTypeFont:
        configured = os.getenv("OVERLAY_FONT_PATH", "").strip()
        bundled = Path(__file__).resolve().parents[2] / "assets" / "NotoSansTC.ttf"
        candidates = [
            configured,
            str(bundled),
            r"C:\Windows\Fonts\msjhbd.ttc",
            r"C:\Windows\Fonts\msjh.ttc",
            r"C:\Windows\Fonts\mingliu.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "DejaVuSans-Bold.ttf",
        ]
        for candidate in candidates:
            if not candidate:
                continue
            try:
                return ImageFont.truetype(candidate, size=size)
            except (OSError, ValueError):
                continue
        raise RuntimeError("找不到可用字型，請設定 OVERLAY_FONT_PATH。")
