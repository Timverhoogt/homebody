"""Keep public entry pages aligned with purpose/transport and release boundaries."""

import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []
        self.ids = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.add(attrs["id"])
        if tag in {"a", "img", "link"}:
            url = attrs.get("href") or attrs.get("src")
            if url:
                self.urls.append(url)


def test_public_pages_separate_purpose_from_speech_and_subscription():
    for name in ("README.md", "index.html"):
        text = (ROOT / name).read_text()
        for concept in ("Everyday", "Workspace", "Kids", "Pipeline", "Realtime", "GPT-Live", "subscription"):
            assert concept.lower() in text.lower(), (name, concept)
    guide = (ROOT / "docs/subscription-reasoning.md").read_text()
    assert "not the default" in guide
    assert "Speech" in guide or "speech" in guide
    assert "not a filesystem sandbox" in guide
    assert 'execution_enabled":false' in guide


def test_landing_local_links_and_assets_exist():
    parser = Links()
    parser.feed((ROOT / "index.html").read_text())
    for url in parser.urls:
        if url.startswith("#"):
            assert url[1:] in parser.ids, url
        elif not re.match(r"[a-z]+:", url):
            assert (ROOT / url.split("#")[0]).exists(), url
        elif url.startswith("https://github.com/Timverhoogt/homebody/blob/main/"):
            assert (ROOT / url.split("/blob/main/", 1)[1].split("#")[0]).exists(), url


def test_status_snapshot_distinguishes_source_release_and_physical_acceptance():
    text = (ROOT / "docs/release-status.md").read_text()
    concepts = (
        "v0.5.2",
        "after",
        "not a universal",
        "physical",
        "APK",
        "barge-in",
        "synthetic",
        "not a filesystem sandbox",
    )
    for concept in concepts:
        assert concept.lower() in text.lower(), concept
    changelog = (ROOT / "CHANGELOG.md").read_text().split("## [0.5.2]", 1)[0]
    assert "Host subscription routing" in changelog
    assert "#77" in changelog and "#78" in changelog


def test_architecture_is_accessible_and_distinguishes_authority():
    svg = ET.parse(ROOT / "docs/assets/architecture.svg").getroot()
    assert svg.attrib["role"] == "img"
    ids = {el.attrib.get("id") for el in svg.iter()}
    assert set(svg.attrib["aria-labelledby"].split()) <= ids
    labels = " ".join(svg.itertext())
    for term in ("Everyday", "Kids", "Workspace", "Speech", "Subscription", "sandbox", "No private memory"):
        assert term.lower() in labels.lower()


def test_space_metadata_keeps_discovery_and_description_bounds():
    text = (ROOT / "hf-space-metadata.yaml").read_text()
    match = re.search(r"^short_description: (.+)$", text, re.M)
    assert match is not None
    description = match.group(1)
    assert len(description) <= 60
    for field in ("sdk: static", "- reachy_mini", "- reachy_mini_python_app"):
        assert field in text
