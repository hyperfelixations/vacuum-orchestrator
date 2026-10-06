"""Template commands and bounded automatic demand using the global writer."""

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from hashlib import sha256

from ..domain.due import DueState
from ..domain.errors import ConflictError
from ..domain.intents import JobIntent, TargetRef
from ..domain.queue import JobProvenance, OrchestratorState
from ..domain.templates import JobTemplate, requested_operations
from ..domain.types import JobState, ProvenanceKind
from ..ports.command_scope import command_origin
from .room_service import Commit

_LIVE = {
    JobState.QUEUED,
    JobState.DISPATCHING,
    JobState.RUNNING,
    JobState.CANCELING,
    JobState.NEEDS_ATTENTION,
}


class TemplateService:
    """Store templates separately from their immutable job snapshots."""

    def __init__(
        self,
        mutate: Commit,
        clock: Callable[[], datetime],
        id_factory: Callable[[], str],
        eligible_rooms: Callable[[OrchestratorState], tuple[str, ...]],
    ) -> None:
        self._mutate, self._clock, self._id_factory = mutate, clock, id_factory
        self._eligible_rooms = eligible_rooms

    async def async_save(
        self,
        name: str,
        intent: JobIntent,
        *,
        template_id: str | None = None,
        enabled: bool = True,
        automatic: bool = False,
    ) -> str:
        """Create or replace a template while retaining past demand suppression."""
        key = template_id or self._id_factory()

        def save(state: OrchestratorState) -> OrchestratorState:
            previous = state.templates.get(key)
            if template_id is not None and previous is None:
                raise ConflictError("unknown_template")
            return self._stored(state, key, name, intent, enabled, automatic, previous)

        await self._mutate(save)
        return key

    async def async_save_job(
        self, job_id: str, name: str, *, automatic: bool = False
    ) -> str:
        """Store a job's intent as a new enabled template.

        Rooms, the "all rooms" choice, title and note are kept; the occasion,
        deduplication key, origin and execution state are not.
        """
        key = self._id_factory()

        def save(state: OrchestratorState) -> OrchestratorState:
            job = state.jobs.get(job_id)
            if job is None:
                raise ConflictError("unknown_job", job_id)
            intent = replace(job.intent, reason=None, dedupe_key=None)
            return self._stored(state, key, name, intent, True, automatic, None)

        await self._mutate(save)
        return key

    def _stored(
        self,
        state: OrchestratorState,
        key: str,
        name: str,
        intent: JobIntent,
        enabled: bool,
        automatic: bool,
        previous: JobTemplate | None,
    ) -> OrchestratorState:
        normalized = replace(
            state.job_defaults.complete(intent),
            areas=tuple(
                TargetRef(
                    state.room_registry.resolve(target.area_id).room_id,
                    target.map_context,
                )
                for target in intent.areas
            ),
        )
        template = JobTemplate(
            key,
            name,
            normalized,
            self._clock(),
            enabled,
            automatic,
            previous.demand_tokens if previous else {},
        )
        return replace(
            state,
            commit_id=state.commit_id + 1,
            templates={**state.templates, key: template},
        )

    async def async_remove(self, template_id: str) -> None:
        """Delete future demand configuration without changing generated jobs."""

        def remove(state: OrchestratorState) -> OrchestratorState:
            if template_id not in state.templates:
                raise ConflictError("unknown_template")
            return replace(
                state,
                commit_id=state.commit_id + 1,
                templates={
                    key: value
                    for key, value in state.templates.items()
                    if key != template_id
                },
            )

        await self._mutate(remove)

    async def async_create_job(self, template_id: str) -> str:
        """Instantiate the stored intent under the same lock as queue insertion."""
        job_id = self._id_factory()

        def create(state: OrchestratorState) -> OrchestratorState:
            template = state.templates.get(template_id)
            if template is None:
                raise ConflictError("unknown_template")
            if not template.enabled:
                raise ConflictError("template_disabled")
            return state.add_job(
                job_id,
                replace(template.intent, areas=self._targets(state, template)),
                self._clock(),
                origin=command_origin.get(),
                provenance=JobProvenance(ProvenanceKind.TEMPLATE, template_id),
            )

        await self._mutate(create)
        return job_id

    async def async_reset_demand(self, template_id: str) -> None:
        """Explicitly allow another automatic attempt for the current due episode."""

        def reset(state: OrchestratorState) -> OrchestratorState:
            template = state.templates.get(template_id)
            if template is None:
                raise ConflictError("unknown_template")
            return replace(
                state,
                commit_id=state.commit_id + 1,
                templates={
                    **state.templates,
                    template_id: replace(template, demand_tokens={}),
                },
            )

        await self._mutate(reset)

    async def async_generate_due(self) -> None:
        """Generate at most one job per room and demand episode, atomically."""

        def generate(state: OrchestratorState) -> OrchestratorState:
            now = self._clock()
            updated = state
            templates = dict(state.templates)
            for template_id, template in state.templates.items():
                if not template.enabled or not template.automatic:
                    continue
                operations = requested_operations(template.intent.mode)
                tokens = dict(template.demand_tokens)
                targets = (
                    tuple(TargetRef(room) for room in self._eligible_rooms(state))
                    if template.intent.all_rooms
                    else template.intent.areas
                )
                for target in targets:
                    room = state.room_registry.resolve(target.area_id)
                    if not room.enabled or room.area_missing:
                        continue
                    reports = [
                        room.due(operation, now).state for operation in operations
                    ]
                    due = DueState.DUE in reports
                    if not due:
                        if all(
                            value in {DueState.FRESH, DueState.DISABLED}
                            for value in reports
                        ):
                            tokens.pop(room.room_id, None)
                        continue
                    covered = frozenset(
                        operation
                        for job in updated.jobs.values()
                        if job.state in _LIVE
                        and any(
                            item.area_id == room.room_id for item in job.intent.areas
                        )
                        for operation in requested_operations(job.intent.mode)
                    )
                    if covered & operations:
                        continue
                    evidence = tuple(
                        (
                            operation.value,
                            room.last_cleaning[operation].receipt_id
                            if operation in room.last_cleaning
                            else None,
                        )
                        for operation in sorted(operations)
                    )
                    token = sha256(
                        repr(
                            (
                                room.room_id,
                                evidence,
                                room.occupancy.epoch,
                                room.due_policy,
                                template.intent.mode,
                            )
                        ).encode()
                    ).hexdigest()
                    if room.room_id in tokens:
                        continue
                    intent = replace(
                        template.intent,
                        areas=(target,),
                        all_rooms=False,
                        dedupe_key=f"due:{template_id}:{token}",
                    )
                    updated = updated.add_job(
                        self._id_factory(),
                        intent,
                        now,
                        provenance=JobProvenance(ProvenanceKind.AUTOMATIC, template_id),
                    )
                    tokens[room.room_id] = token
                templates[template_id] = replace(template, demand_tokens=tokens)
            if updated is state and templates == state.templates:
                return state
            return replace(updated, commit_id=state.commit_id + 1, templates=templates)

        await self._mutate(generate)

    def _targets(
        self, state: OrchestratorState, template: JobTemplate
    ) -> tuple[TargetRef, ...]:
        if not template.intent.all_rooms:
            return template.intent.areas
        rooms = self._eligible_rooms(state)
        if not rooms:
            raise ConflictError("no_eligible_rooms")
        return tuple(TargetRef(room) for room in rooms)
