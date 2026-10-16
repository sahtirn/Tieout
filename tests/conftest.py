from __future__ import annotations

from pathlib import Path

import pytest

from tieout.generator import generate, write_dataset


@pytest.fixture(scope="session")
def dataset_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("seed42")
    write_dataset(generate(42, "2026-09"), out)
    return out


@pytest.fixture()
def loaded_warehouse(dataset_dir: Path):  # type: ignore[no-untyped-def]
    from tieout.io import load_bank_csv, load_gl_csv
    from tieout.warehouse import DuckDBWarehouse

    wh = DuckDBWarehouse()
    wh.load_bank("r1", load_bank_csv(dataset_dir / "bank_statement.csv").transactions)
    wh.load_gl("r1", load_gl_csv(dataset_dir / "gl_cash.csv").entries)
    yield wh
    wh.close()
