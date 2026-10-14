"""Test homes stay synthetic: English names and round run timings.

See dev doc "Testarchitektur": nothing here mirrors a private home; private
history exports contribute only the order in which a robot reports its steps.
"""

from tests.realistic import AREAS, generic, roborock


def test_homes_use_the_synthetic_names() -> None:
    assert AREAS == ("Dining Room", "Studio", "Library", "Conservatory")
    assert [name for _flag, name, _rooms in roborock.MAPS] == [
        "Main Floor",
        "Lower Floor",
    ]
    assert set(roborock.MAPPING) == set(AREAS)
    assert set(generic.MAPPING) < set(AREAS)
    rooms = [room for _flag, _name, rooms in roborock.MAPS for room in rooms.values()]
    assert set(rooms) <= set(AREAS)


def test_runs_last_whole_half_minutes_and_signal_within_seconds() -> None:
    for duration in roborock.DURATIONS:
        assert duration % 30 == 0, duration
    for signal in roborock.SIGNALS:
        assert 0 < signal <= 15, signal
