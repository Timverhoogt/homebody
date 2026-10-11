"""Add Hugging Face metadata only to the exported Space README."""

from __future__ import annotations

import argparse
from pathlib import Path


def prepare(directory: Path) -> None:
    readme = directory / "README.md"
    body = readme.read_text(encoding="utf-8")
    metadata = (directory / "hf-space-metadata.yaml").read_text(encoding="utf-8").strip()
    if body.startswith("---\n"):
        raise ValueError("Space export README already has front matter")
    if not metadata or "---" in metadata.splitlines():
        raise ValueError("Expected metadata fields without YAML document delimiters")
    readme.write_text(f"---\n{metadata}\n---\n\n{body}", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    prepare(parser.parse_args().directory)
