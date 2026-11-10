from __future__ import annotations

import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps


class ImageTextComposer:
    def __init__(self, max_dimension: int | None = None):
        configured_max = int(os.getenv("OVERLAY_MAX_DIMENSION", "0"))
        self.max_dimension = max(0, configured_max if max_dimension is None else max_dimension)
        self.jpeg_quality = min(
            95,
            max(70, int(os.getenv("OVERLAY_JPEG_QUALITY", "91"))),
        )

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

        if self.max_dimension > 0:
            image.thumbnail((self.max_dimension, self.max_dimension), Image.Resampling.LANCZOS)
        width, height = image.size
        if width < 120 or height < 120:
            raise ValueError("照片尺寸太小，無法清楚放上文字。")

        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        font, lines, spacing, _text_bbox = self._fit_text(draw, caption, width, height)

        center_x = width / 2
        center_y = height * 0.36

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
        result.save(
            output,
            format="JPEG",
            quality=self.jpeg_quality,
            optimize=False,
            progressive=True,
        )
        return str(output)

    def _fit_text(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        width: int,
        height: int,
    ) -> tuple[ImageFont.FreeTypeFont, list[str], int, tuple[int, int, int, int]]:
        max_width = int(width * 0.82)
        max_height = int(height * 0.48)
        maximum = max(26, min(int(min(width, height) * 0.15), 160))
        minimum = max(18, min(width, height) // 30)

        for font_size in range(maximum, minimum - 1, -4):
            font = self._load_font(font_size, text)
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

        font = self._load_font(minimum, text)
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
            raise ValueError("文字太長，無法完整放入照片上方，請縮短文字後重試。")
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
    def _load_font(size: int, text: str = "勝光山中文") -> ImageFont.FreeTypeFont:
        configured = os.getenv("OVERLAY_FONT_PATH", "").strip()
        assets_dir = Path(__file__).resolve().parents[2] / "assets"
        bundled_tc = assets_dir / "NotoSansTC.ttf"
        bundled_sc = assets_dir / "NotoSansSC.ttf"
        candidates = [
            configured,
            str(bundled_tc),
            str(bundled_sc),
            r"C:\Windows\Fonts\msjhbd.ttc",
            r"C:\Windows\Fonts\msjh.ttc",
            r"C:\Windows\Fonts\msyhbd.ttc",
            r"C:\Windows\Fonts\msyh.ttc",
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
                font = ImageFont.truetype(candidate, size=size)
                try:
                    font.set_variation_by_name("Bold")
                except (OSError, ValueError):
                    pass
                if ImageTextComposer._font_supports_text(font, text):
                    return font
            except (OSError, ValueError):
                continue
        raise RuntimeError("找不到能完整顯示這段文字的中文字型，請縮短文字或設定 OVERLAY_FONT_PATH。")

    @staticmethod
    def _font_supports_text(font: ImageFont.FreeTypeFont, text: str) -> bool:
        missing_mask = font.getmask("\U0010ffff")
        missing_signature = (missing_mask.size, bytes(missing_mask))
        for character in set(text):
            if character.isspace():
                continue
            mask = font.getmask(character)
            if (mask.size, bytes(mask)) == missing_signature:
                return False
        return True

    @staticmethod
    def _font_supports_cjk(font: ImageFont.FreeTypeFont) -> bool:
        return ImageTextComposer._font_supports_text(font, "勝光山中文")
