"""Rubric and pipeline config models, loading, and content hashing.

A config's hash covers the config itself, the rubric content, every prompt
template, and every term list it uses. Anything that can change a score changes
the hash, so "passed eval on this hash" means what it says (principle 5).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from screener import settings
from screener.hashing import hash_value
from screener.prompts import PROMPT_NAMES, load_prompt

Effort = Literal["low", "medium", "high", "xhigh", "max"]
Tier = Literal["strong", "review", "weak"]
TIER_ORDER: list[str] = ["weak", "review", "strong"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- Rubric -----------------------------------------------------------------


class Level(_Strict):
    description: str = Field(min_length=1)
    example: str = Field(min_length=1)


class Criterion(_Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    weight: float = Field(gt=0)
    must_have: bool = False
    # Tags let counterfactual variants declare which criteria they may legitimately move.
    tags: list[str] = Field(default_factory=list)
    job_relatedness: str = Field(min_length=10)
    levels: dict[int, Level]

    @field_validator("levels")
    @classmethod
    def _levels_0_to_3(cls, v: dict[int, Level]) -> dict[int, Level]:
        if sorted(v) != [0, 1, 2, 3]:
            raise ValueError("levels must define exactly 0, 1, 2, 3")
        return v


class LintAcknowledgment(_Strict):
    criterion: str
    rule: str
    field: str
    acknowledged_by: str
    reason: str = Field(min_length=1)


class Rubric(_Strict):
    id: str
    version: int = Field(ge=1)
    role: str
    criteria: list[Criterion] = Field(min_length=1)
    lint_acknowledgments: list[LintAcknowledgment] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_ids(self) -> Rubric:
        ids = [c.id for c in self.criteria]
        if len(ids) != len(set(ids)):
            raise ValueError("criterion ids must be unique")
        return self

    @property
    def ref(self) -> str:
        return f"{self.id}@v{self.version}"

    @property
    def content_hash(self) -> str:
        return hash_value(self.model_dump(mode="json"))

    def criterion(self, criterion_id: str) -> Criterion:
        return next(c for c in self.criteria if c.id == criterion_id)


# --- Pipeline config -------------------------------------------------------


class IntakeConfig(_Strict):
    min_font_pt: float = 4.0
    color_delta_threshold: float = Field(0.05, ge=0, le=1)
    on_hidden_text: Literal["flag_for_review"] = "flag_for_review"
    render_dpi: int = Field(72, ge=36, le=300)


class ParseConfig(_Strict):
    model: str
    effort: Effort | None = "low"
    max_tokens: int = 8000
    retries: int = Field(1, ge=0, le=3)


class FreeTextRedaction(_Strict):
    neutralize_pronouns: bool = True
    term_lists: list[str] = Field(default_factory=list)


class RedactConfig(_Strict):
    fields: list[Literal["name", "contact", "photo", "address", "links"]] = ["name", "contact", "photo", "address", "links"]
    optional: list[Literal["school_names", "graduation_years", "dates"]] = Field(default_factory=list)
    free_text: FreeTextRedaction = FreeTextRedaction()


class ScoreConfig(_Strict):
    model: str
    effort: Effort | None = "medium"
    samples: int = Field(3, ge=1, le=9)
    instability_threshold: int = Field(1, ge=0, le=3)
    flag_tier_straddle: bool = True
    max_tokens: int = 4000


class TierBands(_Strict):
    strong: float = Field(gt=0, le=1)
    review: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def _ordered(self) -> TierBands:
        if self.review >= self.strong:
            raise ValueError("review band must be below strong band")
        return self


class AggregateConfig(_Strict):
    tiers: TierBands
    must_have_cap: Literal["review", "weak"] = "review"


class ReviewConfig(_Strict):
    open_sla_days: int = Field(5, ge=1)
    hold_alert_days: int = Field(14, ge=1)
    blind_first_look: bool = False


class PipelineConfig(_Strict):
    rubric: str = Field(pattern=r"^[A-Za-z0-9_.-]+@v\d+$")
    intake: IntakeConfig = IntakeConfig()
    parse: ParseConfig
    redact: RedactConfig = RedactConfig()
    score: ScoreConfig
    aggregate: AggregateConfig
    review: ReviewConfig = ReviewConfig()


class TermList(_Strict):
    name: str
    terms: list[str]


class LoadedConfig(BaseModel):
    """A config resolved against its rubric, prompts and term lists, with its hash."""

    model_config = ConfigDict(frozen=True)

    config: PipelineConfig
    rubric: Rubric
    yaml_text: str
    term_lists: dict[str, TermList]
    prompt_hashes: dict[str, str]
    hash: str


# --- Loading ----------------------------------------------------------------


def load_rubric(ref: str, configs_dir: Path | None = None) -> Rubric:
    base = configs_dir or settings.configs_dir()
    path = base / "rubrics" / f"{ref}.yaml"
    rubric = Rubric.model_validate(yaml.safe_load(path.read_text()))
    if rubric.ref != ref:
        raise ValueError(f"rubric file {path.name} declares {rubric.ref}, expected {ref}")
    return rubric


def load_term_list(name: str, configs_dir: Path | None = None) -> TermList:
    base = configs_dir or settings.configs_dir()
    return TermList.model_validate(yaml.safe_load((base / "term_lists" / f"{name}.yaml").read_text()))


def compute_config_hash(config: PipelineConfig, rubric: Rubric, prompt_hashes: dict[str, str], term_lists: dict[str, TermList]) -> str:
    return hash_value(
        {
            "config": config.model_dump(mode="json"),
            "rubric": rubric.content_hash,
            "prompts": prompt_hashes,
            "term_lists": {k: hash_value(v.model_dump(mode="json")) for k, v in term_lists.items()},
        }
    )


def load_config_text(yaml_text: str, configs_dir: Path | None = None) -> LoadedConfig:
    config = PipelineConfig.model_validate(yaml.safe_load(yaml_text))
    rubric = load_rubric(config.rubric, configs_dir)
    term_lists = {n: load_term_list(n, configs_dir) for n in config.redact.free_text.term_lists}
    prompt_hashes = {n: load_prompt(n).hash for n in PROMPT_NAMES}
    return LoadedConfig(
        config=config,
        rubric=rubric,
        yaml_text=yaml_text,
        term_lists=term_lists,
        prompt_hashes=prompt_hashes,
        hash=compute_config_hash(config, rubric, prompt_hashes, term_lists),
    )


def load_config(path: Path | str, configs_dir: Path | None = None) -> LoadedConfig:
    return load_config_text(Path(path).read_text(), configs_dir)


def derive_config(loaded: LoadedConfig, updates: dict, configs_dir: Path | None = None) -> LoadedConfig:
    """A new config (with its own hash) from `loaded` with nested `updates` applied. Used by evals."""

    def merge(base: dict, extra: dict) -> dict:
        out = dict(base)
        for k, v in extra.items():
            out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
        return out

    data = merge(loaded.config.model_dump(mode="json"), updates)
    return load_config_text(yaml.safe_dump(data, sort_keys=False), configs_dir)
