from copy import deepcopy

import pytest

from digital_souls_memory.evaluation.report import validate_manifest

pytestmark = pytest.mark.ut


def test_manifest_requires_reproducible_identities(manifest: dict[str, object]) -> None:
    before = deepcopy(manifest)
    validate_manifest(manifest)
    assert manifest == before


@pytest.mark.parametrize("fault", ["missing", "path", "digest", "extensions", "gpu", "dirty"])
def test_manifest_cannot_hide_missing_or_private_metadata(
    manifest: dict[str, object], fault: str
) -> None:
    if fault == "missing":
        del manifest["core_commit"]
    elif fault == "path":
        manifest["llama_cpp_build"] = "/home/synthetic/private/build"
    elif fault == "digest":
        manifest["db_image"] = "latest"
    elif fault == "extensions":
        manifest["db_extensions"] = {"vector": "0.8.2"}
    elif fault == "gpu":
        manifest["gpu_preflight_confirmed"] = False
    else:
        manifest["memory_dirty"] = "false"
    with pytest.raises((ValueError, KeyError)):
        validate_manifest(manifest)
