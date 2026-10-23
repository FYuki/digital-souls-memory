"""Controllable model boundary for context HTTP and persistence tests."""

from collections.abc import Callable
from threading import Event, Lock
from time import monotonic
from typing import TYPE_CHECKING

from digital_souls_memory.memory.extraction import CloudInput, LocalOnly

if TYPE_CHECKING:
    from digital_souls_memory.episodes.domain import EpisodeProposal
    from digital_souls_memory.resident.domain import StateProposal


def eventually(check: Callable[[], bool]) -> None:
    deadline = monotonic() + 5
    wake = Event()
    while monotonic() < deadline:
        if check():
            return
        wake.wait(0.01)
    assert check(), "Synthetic context worker did not reach the observed state"


class StateGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, LocalOnly | CloudInput]] = []
        self.lock = Lock()
        self.started = Event()
        self.release = Event()
        self.release.set()

    def _state(self, route: str, value: LocalOnly | CloudInput) -> "StateProposal":
        from digital_souls_memory.resident.domain import StateProposal

        with self.lock:
            self.calls.append((route, value))
        self.started.set()
        assert self.release.wait(5), "Test did not release the context gateway"
        return StateProposal(
            mood="穏やか",
            relationship_temperature=None,
            current_topic="".join(turn.text for turn in value.turns),
        )

    def update_state_local(self, value: LocalOnly) -> "StateProposal":
        return self._state("local", value)

    def update_state_cloud(self, value: CloudInput) -> "StateProposal":
        return self._state("cloud", value)

    def split_episodes_local(self, value: LocalOnly) -> "EpisodeProposal":
        raise RuntimeError("Synthetic episode failure, independent of State")

    def split_episodes_cloud(self, value: CloudInput) -> "EpisodeProposal":
        raise RuntimeError("Synthetic episode failure, independent of State")


class EpisodeGateway(StateGateway):
    def __init__(self) -> None:
        super().__init__()
        self.episode_calls: list[tuple[str, LocalOnly | CloudInput]] = []
        self.proposal: EpisodeProposal | None = None
        self.fail = False
        self.episode_started = Event()
        self.episode_release = Event()
        self.episode_release.set()

    def _episodes(self, route: str, value: LocalOnly | CloudInput) -> "EpisodeProposal":
        from digital_souls_memory.episodes.domain import EpisodePart, EpisodeProposal, TurnSpan

        with self.lock:
            self.episode_calls.append((route, value))
        self.episode_started.set()
        assert self.episode_release.wait(5), "Test did not release the episode gateway"
        if self.fail:
            raise RuntimeError("Synthetic episode dependency failure")
        if self.proposal is not None:
            return self.proposal
        return EpisodeProposal(
            model="synthetic-model",
            prompt_version="synthetic-v1",
            episodes=tuple(
                EpisodePart(
                    topic=turn.text,
                    turn_span=(TurnSpan(turn_id=turn.turn_id, start=0, end=len(turn.text)),),
                )
                for turn in value.turns
            ),
        )

    def split_episodes_local(self, value: LocalOnly) -> "EpisodeProposal":
        return self._episodes("local", value)

    def split_episodes_cloud(self, value: CloudInput) -> "EpisodeProposal":
        return self._episodes("cloud", value)
