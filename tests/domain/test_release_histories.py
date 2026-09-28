"""Generated release histories preserve grant ownership across arbitrary ordering."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from custom_components.vacuum_orchestrator.domain.releases import (
    ReleaseKind,
    RoomRelease,
)
from custom_components.vacuum_orchestrator.domain.room_registry import RoomRegistry
from custom_components.vacuum_orchestrator.domain.rooms import Room
from custom_components.vacuum_orchestrator.infrastructure.room_codec import (
    decode_room_registry,
    encode_room_registry,
)


class ReleaseHistories(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.now = datetime(2026, 1, 1, tzinfo=UTC)
        self.registry = RoomRegistry({"room": Room("room", "Room")})
        self.identity = 0

    @rule(kind=st.sampled_from(list(ReleaseKind)))
    def grant(self, kind):
        self.identity += 1
        release = RoomRelease(
            f"grant-{self.identity}",
            kind,
            self.now,
            self.now + timedelta(seconds=10) if kind is ReleaseKind.TIMED else None,
        )
        self.registry = self.registry.put_room(
            replace(self.registry.rooms["room"], release=release)
        )

    @rule()
    def admit(self):
        grant = self.registry.rooms["room"].release
        if grant and grant.allows_new_job(self.now):
            self.identity += 1
            self.registry = self.registry.admit(
                f"job-{self.identity}", ("room",), self.now
            )

    @rule(started=st.booleans(), finish=st.booleans())
    def advance_job(self, started, finish):
        if not self.registry.admissions:
            return
        job = next(iter(self.registry.admissions))
        if started:
            self.registry = self.registry.mark_started(job)
        if finish:
            grant = self.registry.rooms["room"].release
            admission = self.registry.admissions[job][0]
            self.registry = self.registry.finish(job, never_started=not started)
            if grant and grant.grant_id != admission.grant_id:
                assert self.registry.rooms["room"].release == grant

    @rule(seconds=st.integers(min_value=0, max_value=20))
    def tick(self, seconds):
        self.now += timedelta(seconds=seconds)

    @invariant()
    def storage_and_permissions_remain_consistent(self):
        assert (
            decode_room_registry(encode_room_registry(self.registry)) == self.registry
        )
        grant = self.registry.rooms["room"].release
        if grant and grant.consumed:
            assert grant.reserved_job_id and not grant.allows_new_job(self.now)
        for job, admissions in self.registry.admissions.items():
            if admissions[0].started:
                assert self.registry.allows_job(job, "room", self.now)


TestReleaseHistories = ReleaseHistories.TestCase
TestReleaseHistories.settings = settings(
    max_examples=40, stateful_step_count=30, deadline=None, database=None
)
