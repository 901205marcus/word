from __future__ import annotations

import json
import os
from pathlib import Path
from urllib import request

from PIL import Image, ImageDraw, ImageFont


class LineRichMenuManager:
    MENU_NAME = "Marcus Schedule Main Menu v1"
    WIDTH = 2500
    HEIGHT = 843

    def __init__(self, base_dir: str | Path):
        self.base_dir = Path(base_dir)
        self.image_path = self.base_dir / "data" / "line_mvp" / "rich_menu_v1.png"
        self.font_path = self.base_dir / "assets" / "NotoSansTC.ttf"

    def ensure_default(self) -> bool:
        if os.getenv("LINE_RICH_MENU_ENABLED", "true").strip().lower() in {"0", "false", "no"}:
            return False
        token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
        if not token:
            return False

        try:
            for menu in self._list_menus(token):
                if menu.get("name") != self.MENU_NAME:
                    continue
                menu_id = menu.get("richMenuId", "")
                if menu_id and self._set_default(token, menu_id):
                    return True
                if menu_id:
                    self._delete_menu(token, menu_id)

            self._render_image()
            menu_id = self._create_menu(token)
            self._upload_image(token, menu_id)
            if not self._set_default(token, menu_id):
                raise RuntimeError("LINE 拒絕設定預設 Rich Menu")
            return True
        except Exception as exc:
            print(f"Warning: LINE Rich Menu setup failed: {exc}")
            return False

    def _menu_payload(self) -> dict:
        labels = ["行程管理", "圖片加字", "加字狀態", "使用說明"]
        messages = ["行程模式", "加字模式", "加字狀態", "幫助"]
        area_width = self.WIDTH // 4
        areas = []
        for index, (label, message) in enumerate(zip(labels, messages)):
            x = index * area_width
            width = self.WIDTH - x if index == 3 else area_width
            areas.append(
                {
                    "bounds": {"x": x, "y": 0, "width": width, "height": self.HEIGHT},
                    "action": {"type": "message", "label": label, "text": message},
                }
            )
        return {
            "size": {"width": self.WIDTH, "height": self.HEIGHT},
            "selected": True,
            "name": self.MENU_NAME,
            "chatBarText": "選擇功能",
            "areas": areas,
        }

    def _render_image(self) -> Path:
        if not self.font_path.exists():
            raise FileNotFoundError("缺少 NotoSansTC.ttf，無法建立中文字選單")
        self.image_path.parent.mkdir(parents=True, exist_ok=True)
        image = Image.new("RGB", (self.WIDTH, self.HEIGHT), "#f4ead7")
        draw = ImageDraw.Draw(image)
        title_font = self._load_font(76, "Bold")
        number_font = self._load_font(108, "Regular")
        small_font = self._load_font(34, "Medium")
        panels = [
            ("#174f3c", "01", "行程管理", "新增・確認・套用"),
            ("#d15e3b", "02", "圖片加字", "先文字・再照片"),
            ("#276d80", "03", "加字狀態", "查看待配對數量"),
            ("#c39a45", "04", "使用說明", "完整操作指引"),
        ]
        area_width = self.WIDTH // 4
        for index, (color, number, title, subtitle) in enumerate(panels):
            left = index * area_width
            right = self.WIDTH if index == 3 else left + area_width
            inset = 14
            draw.rounded_rectangle(
                (left + inset, inset, right - inset, self.HEIGHT - inset),
                radius=38,
                fill=color,
            )
            center_x = (left + right) // 2
            draw.text((center_x, 160), number, font=number_font, fill="#f9efdc", anchor="mm")
            draw.line((center_x - 95, 255, center_x + 95, 255), fill="#f9efdc", width=5)
            draw.text((center_x, 390), title, font=title_font, fill="white", anchor="mm")
            draw.text((center_x, 520), subtitle, font=small_font, fill="#f9efdc", anchor="mm")
            draw.text((center_x, 690), "點選進入", font=small_font, fill="white", anchor="mm")
        image.save(self.image_path, format="PNG", optimize=True)
        return self.image_path

    def _load_font(self, size: int, variation: str) -> ImageFont.FreeTypeFont:
        font = ImageFont.truetype(str(self.font_path), size)
        try:
            font.set_variation_by_name(variation)
        except (OSError, ValueError):
            pass
        return font

    def _list_menus(self, token: str) -> list[dict]:
        req = request.Request(
            "https://api.line.me/v2/bot/richmenu/list",
            headers={"Authorization": f"Bearer {token}"},
        )
        with request.urlopen(req, timeout=20) as response:
            return json.loads(response.read().decode("utf-8")).get("richmenus", [])

    def _create_menu(self, token: str) -> str:
        req = request.Request(
            "https://api.line.me/v2/bot/richmenu",
            data=json.dumps(self._menu_payload(), ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with request.urlopen(req, timeout=20) as response:
            menu_id = json.loads(response.read().decode("utf-8")).get("richMenuId", "")
        if not menu_id:
            raise RuntimeError("LINE 未回傳 Rich Menu ID")
        return menu_id

    def _upload_image(self, token: str, menu_id: str) -> None:
        req = request.Request(
            f"https://api-data.line.me/v2/bot/richmenu/{menu_id}/content",
            data=self.image_path.read_bytes(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "image/png",
            },
            method="POST",
        )
        with request.urlopen(req, timeout=30):
            return

    @staticmethod
    def _set_default(token: str, menu_id: str) -> bool:
        req = request.Request(
            f"https://api.line.me/v2/bot/user/all/richmenu/{menu_id}",
            data=b"",
            headers={"Authorization": f"Bearer {token}"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=20) as response:
                return 200 <= response.status < 300
        except Exception:
            return False

    @staticmethod
    def _delete_menu(token: str, menu_id: str) -> None:
        req = request.Request(
            f"https://api.line.me/v2/bot/richmenu/{menu_id}",
            headers={"Authorization": f"Bearer {token}"},
            method="DELETE",
        )
        try:
            with request.urlopen(req, timeout=20):
                return
        except Exception:
            return
