# E2E host

A throwaway Home Assistant process for end-to-end tests: the real frontend,
the real integration from this checkout, simulated robots and, optionally, the
card on a sections dashboard. It never contacts another Home Assistant.

```bash
python -m pip install -r requirements-e2e.txt
python -m tests.e2e_host --household single_roborock --install-voi --card ../vacuum-orchestrator-card/dist/vacuum-orchestrator-card.js
```

Once ready the command prints one JSON line and keeps running until its
standard input closes, then stops Home Assistant and removes the temporary
config (`--config DIR` keeps it):

```json
{"url": "http://127.0.0.1:<port>", "session": "<config>/voi_e2e/session.json", "config": "<config>"}
```

- Home Assistant listens on `127.0.0.1` on a free port, stored as its
  confirmed HTTP config (Home Assistant would revert an unconfirmed `http:`
  from YAML to all interfaces after five minutes). It starts onboarded and
  loads no recorder. Requirements come from `requirements-e2e.txt`
  (`--skip-pip`). Home Assistant reports the installation as unsupported in
  Repairs; that is expected for a Python environment.
- `session.json` holds `url`, `client_id`, `access_token` (30 minutes),
  `refresh_token` and `expires_in` of the owner, from which a test builds the
  frontend's `hassTokens`. It exists only in the temporary config.
- `--install-voi` adds the integration through its config flow; without it the
  user can add it in the frontend.
- Households: `no_robot`, `single_roborock`, `generic_area`, `two_robots`,
  `busy`, the homes of `tests/realistic` with the same names and entity IDs.

## Simulators

`roborock` replaces the core integration in the throwaway instance only. It
registers what core registers for a V1 robot with dock and answers
`roborock.get_maps`, `vacuum.send_command` (`app_segment_clean`, …) and
`vacuum.clean_area`. A commanded run plays the scripts of
`tests/realistic/roborock.py` (`vacuum_run`, `mop_run`) in simulator time.
`voi_sim` is a generic vacuum that cleans HA areas.

WebSocket commands for administrators (`voi_e2e_support`):

| Command | Fields | Effect |
|---|---|---|
| `voi_e2e/advance` | `seconds` | simulator time passes for every robot |
| `voi_e2e/robot` | `robot`, `stimulus`, optional `seconds`, `key`, `value` | `clean`, `pause`, `resume`, `return_home`, `dock_after_run`, `offline`, `online`, `set` (a Roborock role), `delay_commands`, `answer` |
| `voi_e2e/robots` | | activity, availability, roles and recorded commands of every robot |

Each returns the robots as `voi_e2e/robots` does. Integration timers (start
delay, settling) run in real time.

## Tests

`pytest tests/e2e_host` checks the build, the households and that the pins
are the requirements Home Assistant declares. `pytest -m e2e_host --no-cov
tests/e2e_host` starts the host. On Windows the launcher adds
`tests/stubs` and stand-ins for two audio wheels that need a compiler.
