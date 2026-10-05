# Vacuum Orchestrator

[![Status: Preview](https://img.shields.io/badge/Status-Preview-orange)](#before-you-start)
[![Home Assistant 2026.9](https://img.shields.io/badge/Home%20Assistant-2026.9-41BDF5?logo=homeassistant&logoColor=white)](#what-you-need)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Questions](https://img.shields.io/badge/Questions%3F-Join%20the%20community-5865F2)](https://discord.gg/zfGKCVEvwe)

Coordinate your robot vacuums from one cleaning queue in
[Home Assistant](https://www.home-assistant.io/). Choose which rooms may be
cleaned, decide when they are due again, and let an available, compatible robot
pick up the next job. Keep jobs for later without having to start them today.

## Features

- An ordered cleaning queue, with jobs you can add, reorder, pause,
  cancel or retry.
- Four cleaning modes: vacuum, mop, vacuum and mop together, or vacuum first
  and mop afterwards.
- Room permissions that last until revoked, for one job, for a set duration,
  or until the queue run finishes.
- Separate vacuuming and mopping intervals, measured either in elapsed time
  or in time the room is occupied.
- Conditions for doors, passages and robot readiness, checked before cleaning.
- Saved job templates, with optional automatic job creation when a room is due.
- Cleaning history that distinguishes confirmed results from inferred ones.
- Home Assistant actions for scripts and automations. A dashboard card is optional.

## Before you start

**Version 0.1.0 is a preview for controlled testing.** It has not yet completed
real-device acceptance testing or distribution validation. Start with one robot
and one room, and supervise the first cleaning. Avoid running another cleaning
automation against the same robot during the test.

Multiple-robot operation is experimental and has only been simulated; it has
not been validated with multiple physical robots.

Newly imported rooms are **locked**, and automatic job creation is **off** for
new templates. Adding the integration or discovering a robot does not start it.
You can prepare rooms and jobs before connecting a robot.

## What you need

- **Home Assistant 2026.9.0** is the tested baseline. Compatibility with the
  upcoming 2026.10 release has not been validated yet.
- A robot already available in Home Assistant through its own integration,
  such as Roborock or Matter. VOI uses that existing connection.
- Room targeting and stopping supported by that robot, plus a cleaning mode
  VOI can select or one you explicitly configure as fixed.
- Administrator access to configure VOI and issue its cleaning actions.

Roborock support depends on the device family and the features exposed in Home
Assistant. Other robots need compatible Home Assistant room controls. A robot
appearing in discovery does not by itself guarantee room cleaning support.
VOI explains unsupported requests instead of silently cleaning different rooms
or choosing a different mode.

## Installation

### Manual installation

1. Create a Home Assistant backup.
2. Download this repository using **Code → Download ZIP** and extract it.
3. Copy the entire `custom_components/vacuum_orchestrator` folder into your
   Home Assistant configuration folder, under `custom_components`.
   The resulting path must be `custom_components/vacuum_orchestrator/manifest.json`.
4. Restart Home Assistant.
5. Open **Settings → Devices & services → Add integration** and search for
   **Vacuum Orchestrator**.

This installs an integration; there is no dashboard JavaScript resource to add.

### HACS

HACS installation is not currently a validated distribution path for this
preview. Use manual installation for evaluation. This repository is not listed
in the default HACS catalogue.

### Updating

Back up Home Assistant, finish or cancel active cleaning, and pause the queue.
Replace the integration folder with the new version and restart Home Assistant.
Check the queue and any attention messages before resuming. For a downgrade,
restore the matching backup rather than assuming an older version can read newer
saved data.

## Set up your rooms and robots

### 1. Check the discovered robots

VOI imports your Home Assistant areas as rooms and discovers registered vacuum
entities. Open the integration page to add or reconfigure a robot profile.
Choose its vacuum entity and, where supported, its target areas.

Some devices need extra configuration for cleaning modes, power settings or
companion sensors. Use the robot profile's **Advanced** field for these settings.
For a vacuum-only device without a mode selector, `fixed_mode: vacuum` is valid
only if vacuuming really is its configured operating mode.

VOI does not switch robot maps automatically. Select the correct map through
the robot's own controls before scheduling rooms on it.

### 2. Check room mappings

Open **Developer tools → Actions** and run
`vacuum_orchestrator.get_rooms`, then `vacuum_orchestrator.get_robots`.
The responses show your rooms, robot IDs and available room targets. These
actions are also useful when setting up scripts without a custom card.

A Home Assistant area is not automatically the same as a segment on a robot's
map. Check that each room has the intended robot targets. If a mapping is
missing, configure it before releasing the room.

<details>
<summary>Example: assign a room to a Roborock map segment</summary>

Use `update_room` with IDs from your own setup. `ROBOT_ID` is the VOI robot ID
from `get_robots`; `MAP_ID` and `SEGMENT_ID` must identify the actual map and
room on your robot. Do not guess segment numbers.

```yaml
action: vacuum_orchestrator.update_room
data:
  room_id: ROOM_ID
  configuration:
    bindings:
      - robot_id: ROBOT_ID
        map_id: MAP_ID
        target_ids: [SEGMENT_ID]
```

Supplying `bindings` replaces that room's binding list. Include mappings for
every robot you want to retain. A room can contain more than one segment.

</details>

### 3. Decide when the room may be cleaned

Release a room using `vacuum_orchestrator.release_room`:

| Permission | Choose | When it ends |
|---|---|---|
| Until I lock it again | `permanent` | When you revoke or replace the permission |
| One job | `once` | After one job has actually started, including all its cleaning phases |
| For a set time | `timed` | After `duration_seconds`, for example `7200` for two hours |
| This queue run | `queue_run` | When the current queue run, or the next one you start, finishes |

Use `revoke_room` to lock a room again. Expiring or revoking permission stops
new jobs from starting; a job that has already started can finish its remaining
phases. To stop that job, use `cancel_job`.

## Your first cleaning

Replace `ROOM_ID` below with a room ID from `get_rooms`. A linked Home Assistant
area ID is also accepted. In **Developer tools → Actions**, switch to YAML mode
and run each action separately.

**Release the room for this queue run:**

```yaml
action: vacuum_orchestrator.release_room
data:
  room_id: ROOM_ID
  kind: queue_run
```

**Add a vacuuming job:**

```yaml
action: vacuum_orchestrator.create_job
data:
  areas: [ROOM_ID]
  mode: vacuum
  name: First room cleaning
```

**Start processing the queue:**

```yaml
action: vacuum_orchestrator.run_queue
```

Run `vacuum_orchestrator.get_queue` to inspect waiting jobs. `get_job` takes a
`job_id` and shows that job's current state, including after it leaves the
waiting queue. `get_job_execution` explains why a particular robot can or
cannot carry it out. An accepted action does not mean cleaning has finished.

## Everyday use

### Choose a cleaning mode

| Mode | Value for `mode` |
|---|---|
| Vacuum only | `vacuum` |
| Mop only | `mop` |
| Vacuum and mop together | `vacuum_and_mop` |
| Vacuum first, then mop | `vacuum_then_mop` |

The last mode waits for the vacuuming phase to succeed before starting the
mopping phase. Different compatible robots may perform the two phases.

Optional preferences include `vacuum_power`, `mop_intensity`, `mop_route` and
`passes`. Available settings depend on the robot. `settings_policy: strict`
requires the requested preferences; `best_effort` allows unsupported optional
preferences to be omitted. Neither changes your rooms or cleaning mode.

### Manage the queue

| What you want to do | Action and fields |
|---|---|
| Add a job | `create_job`: `areas`, `mode`, optional `name` |
| Edit a waiting job | `update_job`: `job_id` and the fields to change |
| Change its position | `move_job`: `job_id`, `direction: up`, `down`, `top` or `bottom` |
| Start processing | `run_queue` |
| Pause new jobs | `pause_queue` |
| Continue processing | `resume_queue` |
| Cancel one job | `cancel_job`: `job_id` |
| Try a failed or cancelled job again | `retry_job`: `job_id` |
| Remove a waiting or finished job | `delete_job`: `job_id` |

All action names use the prefix `vacuum_orchestrator.`. Pausing lets already
started jobs finish, including their remaining phases. Retrying creates a new
job; a room released for one job needs a new permission for that retry.

Every action can return a response, for example through `response_variable` in
a script. Commands return `api_version`, `commit_id` and the affected IDs, such
as `job_id`.

You can keep adding and moving jobs while the queue is running. Blocked jobs
stay waiting, while other executable jobs can proceed.

### When does a queue run finish?

Once no started jobs remain unresolved and no waiting jobs can currently run,
VOI waits **15 minutes** by default. New executable work resets this waiting
period. After the full quiet period, the queue returns to idle and rooms released
for that queue run are locked again. **Blocked jobs may remain in the queue.**

Pausing keeps the run and its permissions open. After resuming, a new quiet
period is required. Use `configure_queue` with `grace_seconds` to change the
duration for future runs; `900` means 15 minutes, and `0` means no extra wait.

### Add door or robot conditions

Room conditions can require a door to be open or another sensor to have an
allowed state. Unknown or unavailable conditions prevent cleaning from starting.
Check the sensor's actual state in Home Assistant: a contact sensor's `on`
often means open, but use the meaning of your own device.

<details>
<summary>Example: require an open door before cleaning a room</summary>

```yaml
action: vacuum_orchestrator.update_room
data:
  room_id: ROOM_ID
  configuration:
    requirements:
      - entity_id: binary_sensor.example_door
        accepted_states: ["on"]
```

This replaces the room's condition list. Include all conditions you want to
keep. Robot-specific conditions can also be set in the robot profile.

</details>

### Decide when cleaning is due again

Choose a time basis per room: **calendar time** since cleaning, or **occupied
time** measured by an occupancy sensor. Set separate intervals for vacuuming and
mopping. Due status does not unlock rooms or start an idle queue.

<details>
<summary>Example: vacuum every two days and mop every seven days</summary>

```yaml
action: vacuum_orchestrator.update_room
data:
  room_id: ROOM_ID
  configuration:
    due_policy:
      basis: calendar
      vacuum_seconds: 172800
      mop_seconds: 604800
```

For occupied time, choose `basis: occupied` and provide
`occupancy_entity_id: binary_sensor.example_occupancy`. The intervals then count
occupied seconds. Unknown sensor periods and time while Home Assistant was
offline are marked as gaps, not assumed to be unoccupied. Use `null` for an
interval to disable that due calculation.

</details>

### Reuse a job with a template

Save frequently used rooms and settings with `save_template`, then use
`create_job_from_template` to add a copy to the queue.

```yaml
action: vacuum_orchestrator.save_template
data:
  name: Regular vacuuming
  intent:
    areas: [ROOM_ID]
    mode: vacuum
  automatic: false
```

With `automatic: true`, a template can create a job when a room becomes due.
It still respects room permissions, conditions and queue controls. A failed or
cancelled automatic job does not cause endless retries. Inspect the cause first;
`reset_template_demand` with the template's ID allows another automatic demand.

### Show room information in Home Assistant

The integration provides queue summaries and optional room entities. Enable
the room entities you want under **Settings → Devices & services → Entities**:

- A room release switch.
- Last cleaning, elapsed time and due status, separately for vacuuming and mopping.

Room entities are disabled by default. Turning the release switch on grants
permanent permission; turning it off revokes permission. Select the other
permission types through `release_room`.

## Troubleshooting

| What you see | What to check |
|---|---|
| A job remains in the queue | Is its room released? Are the door conditions satisfied? Is the right map selected? Use `get_job_execution` with its `job_id`. |
| No robot can run the job | Check room targeting, supported mode, charge, availability and any required device settings. Discovery alone does not establish compatibility. |
| Queue is paused or idle | Use `resume_queue` or `run_queue` when ready. Creating jobs alone does not start an idle queue. |
| Room became locked after a run | Check whether its permission was `once`, `timed` or `queue_run`. |
| A job needs attention after a restart or lost connection | Check the physical robot first, then the integration's Repair message and `get_queue` recovery information. |
| No new automatic job appears | Check the template is enabled and automatic, the room is due, and there is no overlapping job or demand suppression from an earlier attempt. |
| Room entities are missing | They are disabled by default; enable the ones you want in the entity list. |

Use `resolve_recovery` with the robot ID reported in `get_queue` after an
uncertain run. Only set `confirm_stopped: true` after personally verifying the
robot has stopped. Recovery clears uncertain ownership; it does not mark the
room as successfully cleaned.

### What “derived” cleaning means

For cleaning started by VOI, an observed start followed by a stable normal end
may be recorded as **derived** completion. **Confirmed** means stronger evidence
is available. The room keeps the most recent effective result and the most recent
confirmed result separately. An error, connection loss or timeout is not success.

Cleaning started from a manufacturer's app appears in history, but room values
change only when the room, mode and successful completion are all proven. The
current Home Assistant observation paths normally cannot provide that complete
proof for external runs. A last-cleaned timestamp alone is not enough.

### Collect useful information for a bug report

1. Note the time, Home Assistant and VOI versions, robot model, requested rooms
   and mode, and what you expected to happen.
2. On the integration page, enable **debug logging** before reproducing the
   problem. Disable it afterwards to download the captured logs.
3. Download the integration's **diagnostics before restarting or reloading it**.
   Diagnostics include a recent internal trace; that trace is limited to 512
   entries and is cleared when the integration runtime is replaced.
4. For a blocked job, also collect the response from `get_job_execution`.
5. Review attachments for personal information before sharing them in an issue.

VOI debug logs record commands, job and attempt transitions, queue activity,
device-setting confirmations, observations and verified storage writes.
A command returning successfully does not by itself mean cleaning completed.
Include the diagnostic download from the same runtime: it uses the same anonymous
references as VOI's structured log messages. Repeated unchanged observations are
summarized, and the diagnostic download reports how much of the trace was retained.

VOI's structured messages omit names, notes, raw device data and exception messages.
References change after a reload or restart. Normal action responses and messages
from Home Assistant or other integrations can still contain personal information.
Logs use Home Assistant's logging facilities; VOI does not create a separate
persistent log file. If setup fails, diagnostic download can include the failed
setup trace even when the integration has not finished loading.

See Home Assistant's [debug logging and diagnostics guide](https://www.home-assistant.io/docs/configuration/troubleshooting/)
for help collecting these files.

## Join the community

[![Discord](https://img.shields.io/badge/Discord-Join-5865F2?logo=discord&logoColor=white)](https://discord.gg/zfGKCVEvwe)
[![GitHub](https://img.shields.io/badge/GitHub-Follow-181717?logo=github&logoColor=white)](https://github.com/hyperfelixations)
[![YouTube](https://img.shields.io/badge/YouTube-Subscribe-FF0000?logo=youtube&logoColor=white)](https://www.youtube.com/@hyperfelixations)
[![Instagram](https://img.shields.io/badge/Instagram-Follow-E4405F?logo=instagram&logoColor=white)](https://www.instagram.com/hyperfelixations/)

For questions and setup discussion, join Discord. For reproducible problems,
[open an issue](https://github.com/hyperfelixations/vacuum-orchestrator/issues)
with the information above. Feedback from controlled tests is welcome.

## Links

- [Releases](https://github.com/hyperfelixations/vacuum-orchestrator/releases)
- [Issues](https://github.com/hyperfelixations/vacuum-orchestrator/issues)
- [License](LICENSE) (MIT)
- [Discord](https://discord.gg/zfGKCVEvwe) — questions and discussion
- Elsewhere: [GitHub](https://github.com/hyperfelixations),
  [YouTube](https://www.youtube.com/@hyperfelixations), and
  [Instagram](https://www.instagram.com/hyperfelixations/)
