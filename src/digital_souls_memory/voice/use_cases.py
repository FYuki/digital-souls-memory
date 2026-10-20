"""Approved registration and dynamically restricted retrieval."""

from collections.abc import Sequence

from digital_souls_memory.identity.ports import IdentityStore, LLMGateway, Reviewer, ReviewResult
from digital_souls_memory.voice.domain import VerbatimAnchor, VoiceExemplar, VoiceValidationError
from digital_souls_memory.voice.ports import (
    MaterialText,
    VoiceNotFound,
    VoiceStore,
    VoiceUnavailable,
)


class VoiceService:
    def __init__(
        self,
        store: VoiceStore,
        identity: IdentityStore,
        materials: MaterialText,
        gateway: LLMGateway | None,
        reviewer: Reviewer | None,
    ) -> None:
        self.store = store
        self.identity = identity
        self.materials = materials
        self.gateway = gateway
        self.reviewer = reviewer

    def _accessible(self, value: VoiceExemplar) -> bool:
        if value.turn_id is None:
            return True
        text = self.materials.get_text(value.character_id, value.turn_id)
        return text is not None and value.text in text

    def register_exemplars(
        self, character_id: str, values: Sequence[VoiceExemplar]
    ) -> ReviewResult:
        if not values:
            raise VoiceValidationError("Expected a nonempty initial or additional set")
        for value in values:
            if value.character_id != character_id:
                raise PermissionError("Wrong character")
            if self.identity.get_profile(character_id, value.trait_version) is None:
                raise VoiceNotFound()
            if value.turn_id is not None:
                text = self.materials.get_text(character_id, value.turn_id)
                if text is None:
                    raise VoiceNotFound()
                if value.text not in text:
                    raise VoiceValidationError("Text is not in the available original range")
        with self.store.transaction(character_id) as tx:
            result = tx.get_initial_check()
            if result is None:
                profile = self.identity.get_profile(character_id, values[0].trait_version)
                temperament = self.identity.get_temperament(character_id)
                if profile is None or temperament is None:
                    raise VoiceNotFound()
                if self.gateway is None or self.reviewer is None:
                    raise VoiceUnavailable()
                texts = tuple(v.text for v in values)
                responses = self.gateway.generate_typical_responses(
                    temperament=temperament, profile=profile, exemplars=texts
                )
                result = self.reviewer.review_voice(
                    temperament=temperament, profile=profile, exemplars=texts, responses=responses
                )
                tx.save_initial_check(result)
            for value in values:
                tx.add_exemplar(value)
        return result

    def register_anchor(self, value: VerbatimAnchor) -> tuple[VerbatimAnchor, str]:
        text = self.materials.get_text(value.character_id, value.turn_id)
        if text is None:
            raise VoiceNotFound()
        with self.store.transaction(value.character_id) as tx:
            tx.add_anchor(value)
        return value, text

    def get_exemplar(self, character_id: str, exemplar_id: str) -> VoiceExemplar:
        value = self.store.get_exemplar(character_id, exemplar_id)
        if value is None or value.character_id != character_id or not self._accessible(value):
            raise VoiceNotFound()
        return value

    def list_exemplars(self, character_id: str) -> list[VoiceExemplar]:
        with self.store.list_exemplars(character_id) as values:
            return [
                value
                for value in values
                if value.character_id == character_id and self._accessible(value)
            ]

    def get_anchor(self, character_id: str, anchor_id: str) -> tuple[VerbatimAnchor, str]:
        value = self.store.get_anchor(character_id, anchor_id)
        if value is None or value.character_id != character_id:
            raise VoiceNotFound()
        text = self.materials.get_text(character_id, value.turn_id)
        if text is None:
            raise VoiceNotFound()
        return value, text

    def list_anchors(self, character_id: str) -> list[tuple[VerbatimAnchor, str]]:
        result = []
        with self.store.list_anchors(character_id) as values:
            for value in values:
                if value.character_id == character_id:
                    text = self.materials.get_text(character_id, value.turn_id)
                    if text is not None:
                        result.append((value, text))
        return result
