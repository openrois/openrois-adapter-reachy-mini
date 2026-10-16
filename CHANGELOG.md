# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/2.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **Breaking:** the command takes its settings from `adapter.yaml` instead of flags. The
  flags `--sim`, `--host`, `--connection-mode`, `--emotions`, `--piper` and
  `--piper-voices` are gone, and so are the variables `OPENROIS_GATEWAY_URL`,
  `OPENROIS_ENGINE_ID`, `OPENROIS_REACHY_*` and `OPENROIS_PIPER*`. Put `simulator: true`
  in the file instead of `--sim`, `robot_address` instead of `--host`, and the reaction
  and speech settings in their sections. Only `--gateway-url`, `--engine-id`,
  `--robot-address` and `--log-level` remain as flags, and as `OPENROIS_ADAPTER_*`
  variables.
- **Breaking:** a client connects, then binds a component before it sets its
  parameters or runs its commands, PersonDetection included, and reads the command ids
  from the `execute` result, since the engine assigns them. PersonDetection counts only
  from `start` to `stop`.

### Added

- The `openrois-adapter-reachy-mini` command, which serves one Reachy Mini, the real
  robot or its simulator, to an OpenRoIS gateway under its own engine id. Two adapters
  put two robots behind one gateway. Its settings come from `adapter.yaml`, named by
  `--config` or `OPENROIS_ADAPTER_CONFIG`, with one section per component: it reaches the
  robot at `robot_address` over the network, or the simulator on this machine with
  `simulator: true`. The gateway URL, the engine id, the robot's address and the log
  level can also come from `OPENROIS_ADAPTER_*` variables and flags. The repository ships
  `adapter.yaml` for a Reachy Mini Wireless and `adapter.sim.yaml` for the simulator.
- `time_scale` in `adapter.yaml`, a factor on every duration the adapter times itself:
  the reactions and their blends, the checks of the daemon, the camera frames of
  PersonDetection and the hold of a new count, and the wait after a speech. Below 1 for
  faster runs.
- `python -m openrois_adapter_reachy_mini.workflow`, which runs the whole RoIS workflow
  against one robot behind a gateway and prints each step: connect, get_profile, search,
  bind, set_parameter, execute with two branches at the same time, subscribe and
  notify_event, notify_error and get_error_detail while it stops the robot's daemon, then
  release and disconnect. It runs the same against the simulator and the Reachy Mini
  Wireless.
- `ReachyMiniSystemInformation`, which answers `robot_position` with the yaw of the
  robot's body and `engine_status` with the status of the engine.
- `ReachyMiniReaction`, which plays expressions of the head and the antennas from where
  the robot is: `neutral`, `nod`, `shake`, `happy`, `sad` and `curious`, and the moves of
  a recorded library such as Pollen's emotions. `available_reactions` lists them, and
  `reaction_ref` picks one, `nod` by default.
- `ReachyMiniSpeechSynthesis`, which speaks a text on the robot's speaker while the head
  moves to the voice, with Piper voices through `PiperSynthesizer` or a voice-like tone
  through `ToneSynthesizer`.
- `ReachyMiniPersonDetection`, which from `start` to `stop` emits `person_detected` with
  the number of faces the camera sees, once on each start and then each time that number
  changes and holds. `suspend` holds the events back, and `resume` sends them again, with
  the number if it changed in between.
- `ReachyMiniConnection`, the one connection to a robot's daemon that the components of
  that robot share, a `Resource` the engine opens and closes for them. It checks the
  daemon every `retry` seconds. While the daemon does not answer, the four components
  report ERROR: applications that bound or subscribed to them get `notify_error`,
  `component_status` says ERROR, and their commands and queries are refused. They report
  READY again once the daemon is back, so the adapter and the daemon start in any order.
- The components follow the RoIS rules of the OpenRoIS engine: an application connects,
  then binds a component before it sets its parameters or runs its commands. Reaction and
  SpeechSynthesis take one bind at a time, and PersonDetection and SystemInformation are
  shared. The engine assigns every command id. `stop`, or a new `start`, cuts a running
  reaction or speech off, which ends with ABORT. A command a component does not offer,
  such as `fly`, is answered UNSUPPORTED. Every component but SystemInformation answers
  `start`, `stop`, `suspend` and `resume`: a suspended Reaction or SpeechSynthesis holds
  its next start until `resume`.
