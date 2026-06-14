from __future__ import annotations

import os
from datetime import datetime, timedelta
from urllib.parse import quote_plus

from openai import OpenAI

from .models import ScheduleAction
from .parser import normalize_time

TAIPEI_PREFIXES = ("台北市", "臺北市")
ORIGIN_TEXT = "海山捷運站"


def is_taipei_city_address(address: str) -> bool:
    text = (address or "").strip()
    return any(prefix in text for prefix in TAIPEI_PREFIXES)


def build_google_maps_url(address: str) -> str:
    origin = quote_plus(ORIGIN_TEXT)
    destination = quote_plus(address)
    return (
        "https://www.google.com/maps/dir/?api=1"
        f"&origin={origin}&destination={destination}&travelmode=transit"
    )


class MapGuidanceGenerator:
    def __init__(self, model: str = "gpt-4.1-mini"):
        self.model = model

    def enrich_action(self, action: ScheduleAction) -> ScheduleAction:
        if action.action.value != "add":
            return action
        if not is_taipei_city_address(action.address):
            return action
        if "地圖指引" in action.note or "Google 地圖" in action.note:
            return action

        guidance = self._generate_guidance(action)
        maps_url = build_google_maps_url(action.address)
        parts = [part.strip() for part in [action.note, guidance, f"Google 地圖：{maps_url}"] if part.strip()]
        action.note = " | ".join(parts)
        action.requires_review = True
        return action

    def _generate_guidance(self, action: ScheduleAction) -> str:
        departure = suggest_departure_time(action.time)
        fallback = (
            f"AI地圖指引（待確認）：{departure} 從家出發，"
            f"搭乘捷運自海山捷運站前往 {action.address}，"
            "抵達目的站後再步行前往現場。"
        )

        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            return fallback

        client = OpenAI(api_key=api_key)
        prompt = (
            "你是台北市行程秘書。請用繁體中文，寫一段從海山捷運站出發、"
            "以捷運加步行為主的到達指引。"
            "如果無法確認轉乘細節或出口，請保守表述並加上待確認。"
            "只輸出一小段完整文字，不要條列。"
        )
        user_text = (
            f"活動時間：{action.time or '未提供'}\n"
            f"建議出發時間：{departure}\n"
            f"目的地：{action.address}\n"
            f"行程：{action.event or '未提供'}"
        )
        try:
            response = client.responses.create(
                model=self.model,
                input=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": user_text},
                ],
            )
            text = (getattr(response, "output_text", "") or "").strip()
            return f"AI地圖指引（待確認）：{text}" if text else fallback
        except Exception:
            return fallback


def suggest_departure_time(event_time: str) -> str:
    normalized = normalize_time(event_time)
    try:
        dt = datetime.strptime(normalized, "%H:%M")
    except ValueError:
        return "請於出發前"
    return (dt - timedelta(minutes=60)).strftime("%H:%M")
