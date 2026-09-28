# API v2 consumer fixtures

`api_v2_job.json` fixes the pre-existing job response fields for a queued vacuum
job with one area. `tests/test_public_contract.py` calls the real WebSocket
handler and compares every fixture field. Additive fields are permitted; changed
values, removed fields and new values for existing enums are not silently accepted.

Consumers can reuse this fixture without HA or device credentials. The synthetic
job and timestamps are deterministic. Rich configuration endpoints are exercised
through real action and WebSocket handlers in `tests/test_configuration_api.py`;
they remain separate from the compatibility fixture. Importing these fixtures
into a separate card project is a consumer-side task.
