from __future__ import annotations

from pathlib import Path

import pytest

from tieout.generator import generate, write_dataset


@pytest.fixture(scope="session")
def dataset_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("seed42")
    write_dataset(generate(42, "2026-09"), out)
    return out
