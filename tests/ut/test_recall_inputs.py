from collections.abc import Callable
from typing import cast

import pytest
from digital_souls_memory.recall.inputs import (
    CloudInput,
    InputLabel,
    LocalOnly,
    PublicLabel,
    make_model_input,
)

from tests.evidence_support import INSTRUCTION

pytestmark = pytest.mark.ut


@pytest.mark.parametrize("sensitivity", ["sensitive", "unknown", "Public"])
def test_unknown_or_sensitive_labels_keep_all_materials_local(sensitivity: str) -> None:
    labels = (InputLabel("public", "v1"), InputLabel(sensitivity, "v2"))
    value = make_model_input(
        texts=("公開文", INSTRUCTION), labels=labels, non_sensitive_labels=frozenset({"public"})
    )
    assert isinstance(value, LocalOnly)
    assert value.texts == ("公開文", INSTRUCTION)
    assert value.labels == labels


def test_only_explicit_public_labels_allow_cloud_input() -> None:
    value = make_model_input(
        texts=("公開文",),
        labels=(InputLabel("public", "v1"),),
        non_sensitive_labels=frozenset({"public"}),
    )
    assert isinstance(value, CloudInput)
    assert value.texts == ("公開文",)
    assert all(isinstance(label, PublicLabel) for label in value.labels)
    assert [(v.sensitivity, v.classifier_version) for v in value.labels] == [("public", "v1")]


def test_cloud_input_rejects_relabeling_an_unclassified_local_input(
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    local = LocalOnly(texts=(INSTRUCTION,), labels=(InputLabel("sensitive", "v1"),))
    with rejected():
        CloudInput(texts=local.texts, labels=cast(tuple[PublicLabel, ...], local.labels))


@pytest.mark.parametrize(
    "texts,labels",
    [
        ((), (PublicLabel("public", "v1"),)),
        (("本文",), ()),
    ],
)
@pytest.mark.parametrize("input_type", [LocalOnly, CloudInput])
def test_model_input_cannot_drop_text_or_all_classification_labels(
    texts: tuple[str, ...],
    labels: tuple[PublicLabel, ...],
    input_type: type[LocalOnly] | type[CloudInput],
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    with rejected():
        input_type(texts=texts, labels=labels)


@pytest.mark.parametrize("sensitivity,version", [("", "v1"), ("  ", "v1"), ("public", "\t")])
def test_blank_classification_information_is_rejected(
    sensitivity: str,
    version: str,
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    with rejected():
        make_model_input(
            texts=("本文",),
            labels=(InputLabel(sensitivity, version),),
            non_sensitive_labels=frozenset({"public"}),
        )
