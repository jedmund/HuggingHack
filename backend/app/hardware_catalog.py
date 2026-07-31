from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any


CATALOG_PATH = Path(__file__).with_name("data") / "hardware_catalog.json"


@lru_cache(maxsize=1)
def hardware_catalog() -> dict[str, Any]:
    with CATALOG_PATH.open(encoding="utf-8") as catalog_file:
        catalog = json.load(catalog_file)

    if not isinstance(catalog.get("items"), list) or not catalog["items"]:
        raise ValueError("The hardware catalog is empty or invalid.")
    return catalog
