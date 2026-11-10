from __future__ import annotations

from pathlib import Path
from urllib.request import urlopen


FONT_URL = (
    "https://raw.githubusercontent.com/google/fonts/main/"
    "ofl/notosanstc/NotoSansTC%5Bwght%5D.ttf"
)


def main() -> None:
    target = Path(__file__).resolve().parents[1] / "assets" / "NotoSansTC.ttf"
    if target.exists() and target.stat().st_size > 1_000_000:
        print(f"Overlay font already exists: {target}")
        return

    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urlopen(FONT_URL, timeout=60) as response:
            target.write_bytes(response.read())
        print(f"Downloaded overlay font: {target}")
    except Exception as exc:
        print(f"Warning: could not download overlay font: {exc}")


if __name__ == "__main__":
    main()
