# Reachy Mini - OpenRoIS Adapter

The open reference platform of [OpenRoIS](https://openrois.org), on the
[Pollen Robotics Reachy Mini](https://www.pollen-robotics.com/reachy-mini/): an adapter
that exposes the robot through the OMG RoIS Framework 2.0 interfaces, a simulator so
everything runs without hardware, and a one-command bring-up of the whole stack.

Third-party adapters such as the Kachaka components live in the
[openrois](https://github.com/openrois/openrois) repository. This one is the demonstrator
the project ships: affordable, openly documented hardware that anyone can put behind a
gateway and drive from the SDKs.

| Component | Function | Provides |
|-----------|----------|----------|
| `SystemInformation` | | `robot_position` (body yaw at the origin), `engine_status` |
| `Navigation` | actuation | Head and body pose targets, `reached_target` per target, completion |
| `Reaction` | actuation | Named expressions: `nod`, `shake`, `curious`, `happy`, `sad`, `wave_antennas`, `neutral` |
| `SpeechSynthesis` | actuation | Speaks `speech_text`, completes when the speech ends |
| `SpeechRecognition` | sensing | `speech_recognized` events while subscribed |
| `PersonDetection`, `FaceDetection` | sensing | `person_detected` and `face_detected` when the count changes |
| `VideoStreaming` | function | Stream control (`connect_stream` and friends); media at the camera URL |

## Quickstart

```bash
pip install -e ".[dev]"          # plus the OpenRoIS packages, see Install
openrois-reachy-mini-up --demo   # gateway + simulated robot + a short application
```

The demo discovers the components, makes the robot speak, nod, and look around, then
prints what happened. Without `--demo` the command keeps the gateway up on
`ws://127.0.0.1:8765` for the web inspector or any SDK:

```ts
const client = await RoISClient.connect("ws://127.0.0.1:8765");
const reaction = await client.bindAny("Reaction");
await client.execute(reaction, "execute", [{ name: "reaction_ref", data_type_ref: "string", value: "happy" }]);
```

Head and body targets are JSON objects in millimeters and degrees:

```json
[{ "head": { "pitch": -10, "yaw": 30 }, "body_yaw": 15, "antennas": [20, 20], "duration": 0.8 }]
```

## Install

The OpenRoIS packages are not on PyPI yet. From a clone of the
[openrois](https://github.com/openrois/openrois) repository next to this one:

```bash
pip install -e ../openrois/interfaces/python -e ../openrois/components/core -e ../openrois/core
pip install -e ".[dev]"
pip install -e ".[robot]"        # adds the reachy_mini SDK, for the hardware
```

## On the Robot

```bash
openrois-gateway --port 8765                                   # anywhere on the network
openrois-adapter-reachy-mini --config openrois-profile.yaml --robot    # on or next to the robot
```

Set `robot.simulate: false` in the profile (or pass `--robot` to the bring-up) and, when
the gateway authenticates, `engine.token`. See the
[profile](src/openrois_adapter_reachy_mini/openrois-profile.yaml) for every option.

The hardware backend is written against the public `reachy_mini` SDK and has not been
verified on a robot yet: the maintainers have no unit at hand. Motion goes through
`goto_target`; speech, hearing, and face detection use what the installed SDK exposes and
log a warning otherwise. Reports from real robots are welcome as issues.

## Tests

```bash
pytest -q                        # component tests and the OpenRoIS conformance suite
ruff check src tests && mypy src
```

## License

Apache-2.0. See [LICENSE](LICENSE).
