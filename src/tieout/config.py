"""Runtime configuration (env / .env) and YAML-backed reference config."""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_PACKAGED = Path(__file__).resolve().parent / "_data"


def _resolve(name: str) -> Path:
    """Repo checkout (editable install) first, then the copy packaged into the wheel."""
    repo = PROJECT_ROOT / name
    return repo if repo.is_dir() else _PACKAGED / name


DEFAULT_CONFIG_DIR = _resolve("config")
DEFAULT_PROMPTS_DIR = _resolve("prompts")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    llm_provider: Literal["mock", "gemini"] = "mock"
    gemini_api_key: str = Field(default="", repr=False)
    gemini_model: str = "gemini-3.6-flash"
    gemini_timeout_s: int = 60
    gemini_max_retries: int = 4
    # None keeps the model default. Google recommends the default (1.0) for Gemini 3 models;
    # lower values can cause looping (docs/DECISIONS.md D-029).
    gemini_temperature: float | None = Field(default=None, ge=0, le=2)
    date_window_days: int = Field(default=3, ge=0, le=31)
    review_confidence_threshold: float = Field(default=0.80, ge=0, le=1)
    max_agent_steps: int = Field(default=6, ge=1, le=50)
    db_path: str = "tieout.duckdb"
    runs_dir: str = "runs"
    near_similarity_threshold: float = Field(default=0.78, ge=0, le=1)
    batch_max_items: int = Field(default=5, ge=2, le=8)
    batch_time_budget_ms: int = Field(default=2000, ge=10)
    prompt_version: str = "investigator_v1"
    config_dir: str = str(DEFAULT_CONFIG_DIR)
    prompts_dir: str = str(DEFAULT_PROMPTS_DIR)
    entity_name: str = "TieOut Demo Co. (fictional)"
    account_name: str = "Operating Cash - Demo Bank x0912"

    def snapshot(self) -> dict[str, Any]:
        """Config captured on every run. Secrets are never included."""
        data = self.model_dump()
        data.pop("gemini_api_key", None)
        data["gemini_api_key_set"] = bool(self.gemini_api_key)
        return data


def get_settings(**overrides: Any) -> Settings:
    return Settings(**overrides)


class ModelPrice(BaseModel):
    input_per_million: Decimal
    output_per_million: Decimal


class PricingConfig(BaseModel):
    currency: str = "USD"
    note: str = ""
    models: dict[str, ModelPrice]

    def price_for(self, model: str) -> ModelPrice:
        if model in self.models:
            return self.models[model]
        return self.models.get(
            "default", ModelPrice(input_per_million=Decimal("0"), output_per_million=Decimal("0"))
        )


class Account(BaseModel):
    number: str
    name: str


class AccountsConfig(BaseModel):
    cash: Account
    break_types: dict[str, Account]
    journal_offsets: dict[str, Account]
    default_offset: Account

    def offset_for_journal(self, journal: str) -> Account:
        return self.journal_offsets.get(journal, self.default_offset)


class EvalThresholds(BaseModel):
    tie_out_rate: float
    match_f1: float
    classification_accuracy: float


class AssumptionsConfig(BaseModel):
    minutes_per_matched_item: Decimal
    minutes_per_exception: Decimal
    analyst_hourly_rate: Decimal
    note: str = ""


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a mapping")
    return data


@lru_cache(maxsize=8)
def load_pricing(config_dir: str = str(DEFAULT_CONFIG_DIR)) -> PricingConfig:
    raw = _load_yaml(Path(config_dir) / "pricing.yaml")
    # YAML floats would lose precision, so prices are written as strings and parsed as Decimal.
    return PricingConfig.model_validate(raw)


@lru_cache(maxsize=8)
def load_accounts(config_dir: str = str(DEFAULT_CONFIG_DIR)) -> AccountsConfig:
    return AccountsConfig.model_validate(_load_yaml(Path(config_dir) / "accounts.yaml"))


@lru_cache(maxsize=8)
def load_thresholds(config_dir: str = str(DEFAULT_CONFIG_DIR)) -> dict[str, EvalThresholds]:
    raw = _load_yaml(Path(config_dir) / "eval_thresholds.yaml")
    return {k: EvalThresholds.model_validate(v) for k, v in raw.items()}


@lru_cache(maxsize=8)
def load_assumptions(config_dir: str = str(DEFAULT_CONFIG_DIR)) -> AssumptionsConfig:
    return AssumptionsConfig.model_validate(_load_yaml(Path(config_dir) / "assumptions.yaml"))
