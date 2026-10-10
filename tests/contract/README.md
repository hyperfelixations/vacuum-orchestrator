# Recordings of the integration

`recordings/` holds what a dashboard card exchanges with the real integration
over Home Assistant's WebSocket in realistic homes (`tests/realistic`). Each
file is one scenario from `scenarios.py`. Consumers such as the card replay
them in their tests instead of imitating the integration.

`test_recordings.py` runs every scenario and fails when a recording no longer
matches the committed file. After an intended change, rewrite them and review
the diff like code:

```bash
python -m pytest tests/contract --update-recordings
```

## Format

| Key | Content |
|---|---|
| `provenance` | `format` (1), `api_version`, `voi_commit` the recording was made on, `ha_version`, `frozen_now` |
| `scenario`, `description` | Name and what the recording shows |
| `home` | HA area IDs and vacuum entity IDs by name |
| `steps` | The timeline, oldest first |

Each step has one key:

| Step | Meaning |
|---|---|
| `send` | A message the card sent, as sent |
| `receive` | A frame the card received, as received: results, errors and subscription events in their real order |
| `advance` | Seconds that passed before the next step |
| `home` | A change in the home outside the card, such as the robot starting to clean |

Frames are unchanged except for the message `id`: the recorder interleaves its
own `ping` messages to know when all frames arrived, so IDs are renumbered from
1 in sending order and pings are left out.

## Determinism

Time is frozen at `frozen_now` and moves only with `advance`; every timestamp
has six fractional digits. IDs come from counters instead of chance: VOI IDs
look like `00000000-0000-4000-8000-000000000001`, ULIDs (contexts, config
entries, robots) like `01J00000000000000000000001` and registry IDs like
`00000000000000000000000000000001`. The same code therefore records the same
bytes on every machine.
