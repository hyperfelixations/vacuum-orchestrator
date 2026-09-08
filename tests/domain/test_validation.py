"""Validation tests for the public job-intent contract."""

import pytest

from custom_components.vacuum_orchestrator.domain.errors import ValidationError
from custom_components.vacuum_orchestrator.domain.intents import (
    JobIntent,
    JobIntentPatch,
    TargetRef,
    VendorExtension,
)
from custom_components.vacuum_orchestrator.domain.types import (
    CleaningMode,
    parse_cleaning_mode,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("vacuum", CleaningMode.VACUUM),
        ("vac", CleaningMode.VACUUM),
        ("mop", CleaningMode.MOP),
        ("vacuum_and_mop", CleaningMode.VACUUM_AND_MOP),
        ("vac_and_mop", CleaningMode.VACUUM_AND_MOP),
        ("vacuum_then_mop", CleaningMode.VACUUM_THEN_MOP),
        ("vac_then_mop", CleaningMode.VACUUM_THEN_MOP),
        (CleaningMode.MOP, CleaningMode.MOP),
    ],
)
def test_public_mode_aliases_are_canonical(
    value: object, expected: CleaningMode
) -> None:
    assert parse_cleaning_mode(value) is expected


@pytest.mark.parametrize("value", [None, "combine", "", 4])
def test_unknown_mode_is_rejected(value: object) -> None:
    with pytest.raises(ValidationError, match="invalid_cleaning_mode"):
        parse_cleaning_mode(value)


def test_intent_validates_areas_requirements_and_metadata() -> None:
    with pytest.raises(ValidationError, match="job_requires_area"):
        JobIntent((), CleaningMode.VACUUM)
    with pytest.raises(ValidationError, match="duplicate_area"):
        JobIntent((TargetRef("a"), TargetRef("a")), CleaningMode.VACUUM)
    with pytest.raises(ValidationError, match="contradictory"):
        JobIntent(
            (TargetRef("a"),),
            CleaningMode.VACUUM,
            required_on=("binary_sensor.door",),
            required_off=("binary_sensor.door",),
        )
    with pytest.raises(ValidationError, match="invalid_pass_count"):
        JobIntent((TargetRef("a"),), CleaningMode.VACUUM, passes=0)
    with pytest.raises(ValidationError, match="empty_name"):
        JobIntent((TargetRef("a"),), CleaningMode.VACUUM, name=" ")


def test_value_objects_reject_ambiguous_empty_data() -> None:
    with pytest.raises(ValidationError, match="empty_target"):
        TargetRef(" ")
    with pytest.raises(ValidationError, match="empty_map_context"):
        TargetRef("a", " ")
    with pytest.raises(ValidationError, match="invalid_vendor_extension_namespace"):
        VendorExtension("roborock")
    with pytest.raises(ValidationError, match="invalid_vendor_extension_parameters"):
        VendorExtension("roborock.v1", (("x", 1), ("x", 2)))


def test_job_rejects_mixed_or_partially_unspecified_map_contexts() -> None:
    with pytest.raises(ValidationError, match="mixed_map_contexts"):
        JobIntent(
            (TargetRef("kitchen", "ground"), TargetRef("hall", "upper")),
            CleaningMode.VACUUM,
        )
    with pytest.raises(ValidationError, match="mixed_map_contexts"):
        JobIntent(
            (TargetRef("kitchen", "ground"), TargetRef("hall")),
            CleaningMode.VACUUM,
        )


def test_partial_patch_changes_only_supplied_fields_and_supports_clear() -> None:
    intent = JobIntent(
        (TargetRef("a"),), CleaningMode.VACUUM, name="Kitchen", note="old"
    )
    changed = JobIntentPatch(note=None, passes=2).apply(intent)

    assert changed.note is None
    assert changed.passes == 2
    assert changed.name == "Kitchen"
    with pytest.raises(ValidationError, match="empty_job_update"):
        JobIntentPatch().apply(intent)
