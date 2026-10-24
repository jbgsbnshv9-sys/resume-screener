import shutil

import pytest
import yaml
from pydantic import ValidationError

from screener.config import PipelineConfig, derive_config, load_config, load_config_text
from tests.conftest import CONFIGS, SIM_CONFIG


def test_hash_is_stable(sim_config):
    assert load_config(SIM_CONFIG).hash == sim_config.hash


def test_hash_changes_with_config(sim_config):
    other = derive_config(sim_config, {"score": {"samples": 5}})
    assert other.hash != sim_config.hash


def test_hash_changes_with_rubric_content(tmp_path, sim_config):
    configs = tmp_path / "configs"
    shutil.copytree(CONFIGS, configs)
    path = configs / "rubrics" / "backend-engineer-2026q4@v1.yaml"
    data = yaml.safe_load(path.read_text())
    data["criteria"][0]["weight"] = 4
    path.write_text(yaml.safe_dump(data))
    assert load_config(SIM_CONFIG, configs).hash != sim_config.hash


def test_hash_covers_prompts(sim_config):
    assert set(sim_config.prompt_hashes) == {"parse_system", "parse_user", "resume_block", "score_system", "score_user"}


def test_temperature_is_rejected():
    data = yaml.safe_load(SIM_CONFIG.read_text())
    data["score"]["temperature"] = 0.3
    with pytest.raises(ValidationError):
        PipelineConfig.model_validate(data)


def test_unknown_keys_and_bad_tiers_rejected():
    data = yaml.safe_load(SIM_CONFIG.read_text())
    data["aggregate"]["tiers"] = {"strong": 0.4, "review": 0.6}
    with pytest.raises(ValidationError):
        PipelineConfig.model_validate(data)


def test_rubric_ref_must_match_file(tmp_path):
    configs = tmp_path / "configs"
    shutil.copytree(CONFIGS, configs)
    text = SIM_CONFIG.read_text().replace("@v1", "@v9")
    with pytest.raises(FileNotFoundError):
        load_config_text(text, configs)
