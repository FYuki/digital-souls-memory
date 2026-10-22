"""Identity and approved voice registration stay HTTP operations of the thin client."""

import httpx
import pytest
from fastapi.testclient import TestClient

from digital_souls_memory.api import create_app
from digital_souls_memory.client import MemoryClient, MemoryHTTPError
from digital_souls_memory.client.models import (
    AnchorInput,
    CoreInput,
    ExemplarSetInput,
    SeedInput,
    SessionRequest,
    TemperamentInput,
    TurnRequest,
)
from tests.evidence_support import RECORDED, session_payload, turn_payload
from tests.identity_support import seed_payload
from tests.it1.evidence_fakes import FakeEvidenceStore
from tests.it1.identity_fakes import FakeIdentityStore
from tests.it1.voice_fakes import FakeVoiceStore, VoiceGateway, VoiceReviewer
from tests.voice_support import TEXT, anchor_payload, exemplar_payload

pytestmark = pytest.mark.it1


def test_initial_registration_voice_and_anchor_roundtrip_without_changing_identity() -> None:
    app = create_app(
        store=FakeEvidenceStore(),
        identity_store=FakeIdentityStore(),
        voice_store=FakeVoiceStore(),
        llm_gateway=VoiceGateway(),
        reviewer=VoiceReviewer(),
        clock=lambda: RECORDED,
    )
    with TestClient(app) as server:

        def transfer(request: httpx.Request) -> httpx.Response:
            response = server.request(
                request.method, str(request.url), content=request.content, headers=request.headers
            )
            return httpx.Response(
                response.status_code, content=response.content, headers=response.headers
            )

        with MemoryClient(
            base_url="http://testserver", timeout=3.0, transport=httpx.MockTransport(transfer)
        ) as client:
            core = client.register_core(
                "char-a", CoreInput.model_validate({"values": ["誠実"], "prohibitions": []})
            )
            temperament = client.register_temperament(
                "char-a", TemperamentInput(description="落ち着いて話す")
            )
            profile = client.author_seed("char-a", SeedInput.model_validate(seed_payload()))
            assert core.character_id == temperament.character_id == profile.character_id == "char-a"
            assert list(core.values) == ["誠実"]
            assert temperament.description == "落ち着いて話す"
            assert profile.temperament_version == temperament.version
            before = client.get_identity("char-a")
            assert before.core_identity == core
            assert before.temperament == temperament
            assert before.personality_profile == profile

            client.add_session(
                SessionRequest.model_validate(session_payload() | {"reports_delivery": False})
            )
            client.add_turn(
                TurnRequest.model_validate(
                    turn_payload()
                    | {
                        "actor": "char-a",
                        "perceived_by": ["char-a"],
                        "text": TEXT,
                    }
                )
            )
            client.register_exemplars(
                "char-a", ExemplarSetInput.model_validate({"exemplars": [exemplar_payload()]})
            )
            assert [(v.exemplar_id, v.text) for v in client.list_exemplars("char-a")] == [
                ("voice-1", TEXT),
            ]
            anchor = client.register_anchor("char-a", AnchorInput.model_validate(anchor_payload()))
            assert (anchor.anchor_id, anchor.turn_id, anchor.text) == ("anchor-1", "turn-1", TEXT)
            assert client.get_identity("char-a") == before
            with pytest.raises(MemoryHTTPError) as failed:
                client.author_seed("char-a", SeedInput.model_validate(seed_payload()))
            assert failed.value.status_code == 409
            assert client.get_identity("char-a") == before
