# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/2.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- The `openrois-adapter-reachy-mini` command, which serves one Reachy Mini, the real
  robot or its simulator, to an OpenRoIS gateway under its own engine id. Two adapters put
  two robots behind one gateway. Every flag falls back to an `OPENROIS_*` environment
  variable.
- `ReachyMiniSystemInformation`, the status of the robot and the yaw of its body.
- `ReachyMiniReaction`, which plays expressions of the head and the antennas from where
  the robot is: `neutral`, `nod`, `shake`, `happy`, `sad` and `curious`, and the moves of
  a recorded library such as Pollen's emotions. `available_reactions` lists them.
- `ReachyMiniSpeechSynthesis`, which speaks a text on the robot's speaker while the head
  moves to the voice, with Piper voices through `PiperSynthesizer` or a voice-like tone
  through `ToneSynthesizer`. `stop` cuts the voice off.
- `ReachyMiniPersonDetection`, which emits `person_detected` with the number of faces
  the camera sees each time that number changes and holds.
- `ReachyMiniConnection`, the one connection to a robot's daemon that the components of
  that robot share.
