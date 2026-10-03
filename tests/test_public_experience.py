from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
SPACE_HTML = ROOT / "index.html"


class _AnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.anchors: list[dict[str, str]] = []
        self.images: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self.anchors.append({key: value or "" for key, value in attrs})
        if tag == "img":
            self.images.append({key: value or "" for key, value in attrs})


def test_space_external_links_are_safe_accessible_https_anchors() -> None:
    parser = _AnchorParser()
    parser.feed(SPACE_HTML.read_text(encoding="utf-8"))
    external = [anchor for anchor in parser.anchors if anchor.get("href", "").startswith("http")]

    assert external
    for anchor in external:
        assert urlparse(anchor["href"]).scheme == "https"
        assert anchor.get("target") == "_blank"
        assert set(anchor.get("rel", "").split()) >= {"noopener", "noreferrer"}
        assert "opens in a new tab" in anchor.get("aria-label", "")


def test_space_uses_no_programmatic_external_navigation() -> None:
    html = SPACE_HTML.read_text(encoding="utf-8")

    assert "window.open" not in html
    assert "<iframe" not in html
    assert "http-equiv=\"refresh\"" not in html.lower()


def test_hugging_face_card_metadata_is_valid_for_static_space() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    match = re.search(r"^short_description:\s*(.+)$", readme, re.MULTILINE)

    assert match
    assert len(match.group(1).strip()) <= 60
    assert re.search(r"^sdk:\s*static$", readme, re.MULTILINE)


def test_public_experience_documents_and_architecture_are_linked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    space = SPACE_HTML.read_text(encoding="utf-8")

    for relative_path in (
        "docs/lite-raspberry-pi-4.md",
        "docs/public-image-shot-list.md",
        "docs/IMAGE_CREDITS.md",
        "docs/assets/architecture.svg",
        "docs/assets/lite-pi-overview.svg",
        "docs/assets/hero-reachy.webp",
        "docs/assets/reachy-components.webp",
        "docs/assets/lite-assembly.webp",
        "docs/assets/mic-camera.webp",
        "docs/assets/ui-dashboard.webp",
        "docs/assets/ui-robot.webp",
    ):
        assert (ROOT / relative_path).is_file()
        assert relative_path in readme or relative_path in space

    ET.parse(ROOT / "docs/assets/architecture.svg")
    ET.parse(ROOT / "docs/assets/lite-pi-overview.svg")
    assert "Supported setups" in readme
    assert "docs/hardware-setups.md" in readme
    setups = (ROOT / "docs/hardware-setups.md").read_text(encoding="utf-8")
    for setup in ("Lite + PC or Mac", "Lite + Raspberry Pi", "Lite + NVIDIA Jetson Orin Nano", "Reachy Mini Wireless"):
        assert setup in setups
    assert "What can I expect?" in readme
    assert "community" in (ROOT / "docs/lite-raspberry-pi-4.md").read_text(encoding="utf-8").lower()


def test_public_images_are_accessible_lightweight_and_credited() -> None:
    parser = _AnchorParser()
    parser.feed(SPACE_HTML.read_text(encoding="utf-8"))

    assert len(parser.images) >= 6
    for image in parser.images:
        assert image.get("src")
        assert image.get("alt", "").strip()
        path = ROOT / image["src"]
        assert path.is_file(), path
        assert path.stat().st_size < 500_000, path

    credits = (ROOT / "docs/IMAGE_CREDITS.md").read_text(encoding="utf-8")
    assert "1e8a628cce504e49c86a982dd1a59f311b03cefc" in credits
    assert "Apache License 2.0" in credits
    assert "no Pollen Robotics endorsement" in credits
    assert "not evidence of a live robot connection" in credits


def test_dense_diagrams_have_mobile_scroll_affordances_and_text_equivalents() -> None:
    html = SPACE_HTML.read_text(encoding="utf-8")
    css = (ROOT / "style.css").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert html.count('class="diagram-scroll"') == 2
    assert html.count('class="diagram-hint"') == 2
    assert html.count('class="diagram-summary"') == 2
    assert len(re.findall(r'class="diagram-scroll"[^>]*tabindex="0"', html)) == 2
    assert "overflow-x: auto" in css
    assert css.count("min-width: 900px") == 2
    assert "Tap or open the diagram for its full-size labels" in readme


def test_public_material_contains_no_environment_specific_network_details() -> None:
    public_files = (
        ROOT / "README.md",
        ROOT / "index.html",
        ROOT / "docs/lite-raspberry-pi-4.md",
        ROOT / "docs/public-image-shot-list.md",
        ROOT / "docs/assets/architecture.svg",
    )
    forbidden = re.compile(
        r"(?:192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
        r"tail[0-9a-z.-]*\.ts\.net|sk-[A-Za-z0-9_-]{12,})",
        re.IGNORECASE,
    )

    for path in public_files:
        assert forbidden.search(path.read_text(encoding="utf-8")) is None, path
