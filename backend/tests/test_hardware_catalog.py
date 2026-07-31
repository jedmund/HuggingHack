from app.hardware_catalog import hardware_catalog


def test_hardware_catalog_has_unique_selectable_models():
    catalog = hardware_catalog()
    items = catalog["items"]

    assert len(items) >= 200
    assert len({item["id"] for item in items}) == len(items)
    assert {item["kind"] for item in items} == {"cpu", "gpu", "apple_silicon"}
    assert all(item["vendor"] and item["model"] for item in items)
    assert all(memory > 0 for item in items for memory in item["memory_gb"])


def test_hardware_catalog_includes_common_memory_variants():
    items = {item["id"]: item for item in hardware_catalog()["items"]}

    assert items["gpu-nvidia-rtx-4090"]["memory_gb"] == [24]
    assert items["gpu-nvidia-rtx-3060"]["memory_gb"] == [8, 12]
    assert items["apple-silicon-apple-apple-m4-max"]["memory_gb"] == [36, 48, 64, 128]
