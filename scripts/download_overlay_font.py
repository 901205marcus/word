from __future__ import annotations

from pathlib import Path
from urllib.request import urlopen


FONTS = {
    "NotoSansTC.ttf": (
        "https://raw.githubusercontent.com/google/fonts/main/"
        "ofl/notosanstc/NotoSansTC%5Bwght%5D.ttf"
    ),
    "NotoSansSC.ttf": (
        "https://raw.githubusercontent.com/google/fonts/main/"
        "ofl/notosanssc/NotoSansSC%5Bwght%5D.ttf"
    ),
}


def main() -> None:
    assets_dir = Path(__file__).resolve().parents[1] / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    for filename, url in FONTS.items():
        target = assets_dir / filename
        if target.exists() and target.stat().st_size > 1_000_000:
            print(f"Overlay font already exists: {target}")
            continue
        try:
            with urlopen(url, timeout=60) as response:
                target.write_bytes(response.read())
            print(f"Downloaded overlay font: {target}")
        except Exception as exc:
            raise SystemExit(f"Could not prepare required overlay font {filename}: {exc}") from exc

        if target.stat().st_size <= 1_000_000:
            raise SystemExit(f"Downloaded overlay font is unexpectedly small: {filename}")


if __name__ == "__main__":
    main()
