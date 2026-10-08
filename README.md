# OpenRoIS Adapter for Reachy Mini

The [OpenRoIS](https://openrois.org) adapter for the Pollen Robotics
[Reachy Mini](https://huggingface.co/docs/reachy_mini). It serves canonical RoIS components
for a Reachy Mini, the real robot or its simulator, so that a service application drives
the robot through an OpenRoIS gateway with the standard RoIS interfaces.

## Status

In progress. The adapter is being rebuilt on `openrois-engine` and
`openrois-components-core`. Its first release, 0.1.0a1, comes together with the first
releases of those packages and of `openrois-gateway`, once one HRI client has driven a
Reachy Mini Wireless and the simulator through one gateway.

The components, all canonical RoIS types (planned):

| Component | On the Reachy Mini |
|-----------|--------------------|
| SystemInformation | The status of the robot and its body yaw |
| Reaction | Expressions of the head, the antennas and the body |
| SpeechSynthesis | A text spoken on the speaker, with the head moving to the voice |
| PersonDetection | The number of faces the camera sees |

## Layout

- `src/openrois/components/reachy_mini/`: the components, imported as
  `openrois.components.reachy_mini`. Once validated, they move to the openrois repository
  as `openrois-components-reachy-mini`, under the same import path.
- `src/openrois_adapter_reachy_mini/`: the adapter process (planned), one per robot.
- `scripts/spike.py`: checks how the installed Reachy Mini SDK behaves on the robot or
  the simulator, and writes a JSON report.

## Development

Python 3.12 or later. Until their first release, the OpenRoIS packages come from the
openrois repository at a pinned commit.

```bash
python -m pip install -e ".[dev]"
ruff check src tests scripts
mypy
pytest
```

To work with a robot or the simulator, add the Reachy Mini SDK at the version of the
robot's daemon, 1.9 or later:

```bash
python -m pip install -e ".[dev,robot]"
```

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
