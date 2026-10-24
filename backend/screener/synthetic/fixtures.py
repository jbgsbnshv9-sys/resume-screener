"""The synthetic fixture suite: clean resumes at each tier, every hidden-text kind,
visible injection, image-only, two-column, and DOCX (clean and with hidden runs)."""

from __future__ import annotations

import random
from pathlib import Path

from screener.synthetic.personas import Persona, generate_persona
from screener.synthetic.render import VISIBLE_INJECTION, RenderOptions, render_docx, render_pdf

HIDDEN_KINDS = ("white", "tiny", "offpage", "covered", "zero_opacity", "metadata")


def fixture_personas(seed: int = 11) -> dict[str, Persona]:
    rng = random.Random(seed)
    return {tier: generate_persona(f"fixture-{tier}", tier, rng) for tier in ("strong", "review", "weak")}


def fixture_files(seed: int = 11) -> dict[str, bytes]:
    people = fixture_personas(seed)
    strong = people["strong"]
    files: dict[str, bytes] = {f"clean_{tier}.pdf": render_pdf(p) for tier, p in people.items()}
    for kind in HIDDEN_KINDS:
        files[f"hidden_{kind}.pdf"] = render_pdf(people["weak"], RenderOptions(hidden=[kind]))
    injected = people["weak"].model_copy(update={"summary": VISIBLE_INJECTION})
    files["injection_visible.pdf"] = render_pdf(injected)
    files["image_only.pdf"] = render_pdf(strong, RenderOptions(image_only=True))
    files["two_column.pdf"] = render_pdf(strong, RenderOptions(layout="two_column"))
    files["clean_strong.docx"] = render_docx(strong)
    files["hidden_runs.docx"] = render_docx(people["weak"], hidden_vanish=True)
    return files


def write_fixtures(out: Path, seed: int = 11) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, data in fixture_files(seed).items():
        path = out / name
        path.write_bytes(data)
        paths.append(path)
    return paths
