"""Classification-preserving model inputs."""

from dataclasses import dataclass


@dataclass(frozen=True)
class InputLabel:
    sensitivity: str
    classifier_version: str

    def __post_init__(self) -> None:
        if not self.sensitivity.strip() or not self.classifier_version.strip():
            raise ValueError("Classification is required")


@dataclass(frozen=True)
class PublicLabel(InputLabel):
    """An explicitly allowed non-sensitive classification."""


@dataclass(frozen=True, kw_only=True)
class LocalOnly:
    texts: tuple[str, ...]
    labels: tuple[InputLabel, ...]

    def __post_init__(self) -> None:
        if not self.texts or not self.labels:
            raise ValueError("Texts and classifications are required")


@dataclass(frozen=True, kw_only=True)
class CloudInput:
    texts: tuple[str, ...]
    labels: tuple[PublicLabel, ...]

    def __post_init__(self) -> None:
        if (
            not self.texts
            or not self.labels
            or any(not isinstance(v, PublicLabel) for v in self.labels)
        ):
            raise ValueError("Cloud input requires explicit public classifications")


type ModelInput = LocalOnly | CloudInput


def make_model_input(
    *, texts: tuple[str, ...], labels: tuple[InputLabel, ...], non_sensitive_labels: frozenset[str]
) -> ModelInput:
    if labels and all(v.sensitivity in non_sensitive_labels for v in labels):
        return CloudInput(
            texts=texts,
            labels=tuple(PublicLabel(v.sensitivity, v.classifier_version) for v in labels),
        )
    return LocalOnly(texts=texts, labels=labels)
