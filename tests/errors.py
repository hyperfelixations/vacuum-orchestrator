"""Assert translated service errors by their stable code."""

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from homeassistant.exceptions import ServiceValidationError

from custom_components.vacuum_orchestrator.const import DOMAIN


@contextmanager
def raises_code(code: str) -> Iterator[None]:
    with pytest.raises(ServiceValidationError) as info:
        yield
    assert info.value.translation_domain == DOMAIN
    assert info.value.translation_key == code
