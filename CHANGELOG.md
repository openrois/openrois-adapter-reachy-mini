# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/2.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Adapter on the Python adapter SDK with eight RoIS components for the Reachy Mini:
  `SystemInformation`, `Navigation` (head and body pose as the target space), `Reaction`
  (named expressions), `SpeechSynthesis`, `SpeechRecognition`, `PersonDetection`,
  `FaceDetection`, and `VideoStreaming` (control plane).
- `SimulatedBackend`: a kinematic model of the robot with scripted hearing and detection,
  so the whole stack runs and is tested without hardware. `ReachyMiniBackend` drives the
  robot through the `reachy_mini` SDK (not yet verified on hardware).
- `openrois-adapter-reachy-mini`: the adapter process. `openrois-reachy-mini-up`: gateway plus
  adapter in one command, with `--demo` for a scripted application.
- The OpenRoIS conformance suite and component tests in CI, in simulation.
