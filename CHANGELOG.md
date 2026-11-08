# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/2.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0a1] - 2026-11-08

### Added

- The `openrois-adapter-reachy-mini` command, which serves one Reachy Mini, the real
  robot or its simulator, to an OpenRoIS gateway under its own engine id, on Python 3.12
  or later. Two adapters put two robots behind one gateway. Its settings come from
  `adapter.yaml`, named by `--config` or `OPENROIS_ADAPTER_CONFIG`, with one section per
  component: it reaches the robot at `robot_address` over the network, an IP address or a
  `.local` name, or the simulator on this machine with `simulator: true`. The gateway URL,
  the engine id, the robot's address and the log level can also come from
  `OPENROIS_ADAPTER_*` variables and flags. The README gives `adapter.yaml` for a Reachy
  Mini Wireless and `adapter.sim.yaml` for the simulator, which the repository and the
  source distribution also ship. A setting the adapter does not know, a value out of
  range, a Piper voices folder without voices, or a `piper` command it cannot run stops
  it with a message that names the setting. It warns when `robot_address` is a name that
  Reachy Mini daemons 1.12 and later refuse.
- `time_scale` in `adapter.yaml`, a factor on every duration the adapter times itself:
  the reactions and their blends, the checks of the daemon, the camera frames of
  PersonDetection and the hold of a new count, and the wait after a speech. Below 1 for
  faster runs.
- `python -m openrois_adapter_reachy_mini.workflow`, which runs the whole RoIS workflow
  against one robot behind a gateway and prints each step: connect, get_profile, search,
  bind, set_parameter, execute with two branches at the same time, subscribe and
  notify_event, notify_error and get_error_detail while it stops the robot's daemon, then
  release and disconnect. It runs the same against the simulator and the Reachy Mini
  Wireless. It prints the daemon's media status, asks whether the robot was heard after
  the speech unless `--yes` is given, and names a component that another application,
  such as an MCP server, holds.
- `ReachyMiniSystemInformation`, which answers `robot_position` with the yaw of the
  robot's body and `engine_status` with the status of the engine.
- `ReachyMiniReaction`, which plays expressions of the head and the antennas from where
  the robot is: `neutral`, `nod`, `shake`, `happy`, `sad` and `curious`, and the moves of
  a recorded library such as Pollen's emotions, without their sound. `available_reactions`
  lists them, and `reaction_ref` picks one, `nod` by default. The move plays in a worker
  thread, so a Wi-Fi that stalls never holds up the other components or the stop.
- `ReachyMiniSpeechSynthesis`, which speaks a text on the robot's speaker while the head
  moves to the voice, with Piper voices through `PiperSynthesizer` or a voice-like tone
  through `ToneSynthesizer`. The default `character` is the first of `available_voices`,
  and `volume`, 100 by default, is the share of the voice's full level, so the voice
  never clips. For a robot on the network the daemon plays the sound, and a start whose
  sound the daemon refuses, or whose daemon has no media running, ends with ERROR. An
  upload that loses its connection, or whose robot name does not resolve, is tried once
  more. A stop cuts the voice off also while the sound is still on its way to the robot.
- `ReachyMiniPersonDetection`, which from `start` to `stop` emits `person_detected` with
  the number of faces the camera sees, once on each start and then each time that number
  changes and holds for `hold` seconds, two by default. Frames are 640 pixels wide for the
  detector, and a face that the edge of the frame cuts off does not count unless
  `ignore_edge_faces` is false. `suspend` holds the events back, and `resume` sends them
  again, with the number if it changed in between. Until the face detector's model can be
  had from the Hugging Face Hub or its cache, the component reports ERROR with the reason
  and keeps trying.
- `ReachyMiniConnection`, the one connection to a robot's daemon that the components of
  that robot share, a `Resource` the engine opens and closes for them. It checks the
  daemon every `retry` seconds. While the daemon does not answer, the four components
  report ERROR: applications that bound or subscribed to them get `notify_error`,
  `component_status` says ERROR, and their commands and queries are refused. They report
  READY again once the daemon is back, so the adapter and the daemon start in any order,
  and whatever the SDK raises while it connects, the connection keeps trying. A state
  stream that pauses for less than `lost_after` seconds raises no error and does not
  reconnect.
- `ReachyMiniMedia`, the camera and the speaker of a robot, which PersonDetection and
  SpeechSynthesis use next to the connection. While the media fail and the daemon
  answers, only those two report ERROR, and the robot still moves. A robot whose daemon
  reports no Wi-Fi address never streams the camera or plays on the speaker of the
  machine the adapter runs on, such as a simulator's.
- The components follow the RoIS rules of the OpenRoIS engine: an application connects,
  then binds a component before it sets its parameters or runs its commands. Reaction and
  SpeechSynthesis take one bind at a time, and PersonDetection and SystemInformation are
  shared. The engine assigns every command id. A value a component refuses, such as an
  unknown `reaction_ref` or a `volume` above 100, is answered BAD_PARAMETER, and no
  command runs. `stop`, or a new `start`, cuts a running reaction or speech off, which
  ends with ABORT. A command a component does not offer, such as `fly`, is answered
  UNSUPPORTED. Every component but SystemInformation answers `start`, `stop`, `suspend`
  and `resume`: a suspended Reaction or SpeechSynthesis holds its next start until
  `resume`.

[unreleased]: https://github.com/openrois/openrois-adapter-reachy-mini/compare/v0.1.0-alpha.1...HEAD
[0.1.0a1]: https://github.com/openrois/openrois-adapter-reachy-mini/releases/tag/v0.1.0-alpha.1
