"""Versioned prompt templates, hashed at load.

Changing a template changes its hash, which changes every config hash that uses
it. Never edit a template to "fix" a stored run; stored runs keep the hash of
the template they used.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from string import Template

from pydantic import BaseModel

from screener.hashing import sha256_text

PROMPT_NAMES = ("parse_system", "parse_user", "resume_block", "score_system", "score_user")
_DIR = Path(__file__).parent


class Prompt(BaseModel):
    name: str
    text: str
    hash: str

    def render(self, **values: str) -> str:
        return Template(self.text).substitute(**values)


@cache
def load_prompt(name: str) -> Prompt:
    if name not in PROMPT_NAMES:
        raise KeyError(name)
    text = (_DIR / f"{name}.md").read_text()
    return Prompt(name=name, text=text, hash=sha256_text(text))
