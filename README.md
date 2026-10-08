# OpenRoIS Adapter for Reachy Mini

The [OpenRoIS](https://openrois.org) adapter for the Pollen Robotics
[Reachy Mini](https://huggingface.co/docs/reachy_mini). It serves canonical RoIS components
for a Reachy Mini, the real robot or its simulator, so that a service application drives
the robot through an OpenRoIS gateway with the standard RoIS interfaces.

## Status

In progress. The four components and the adapter command are in place, and the SDK
calls they rely on were checked on a Reachy Mini Wireless and on the simulator. The first
release, 0.1.0a1, comes together with the first releases of `openrois-engine`,
`openrois-components-core` and `openrois-gateway`, once one HRI client has driven a
Reachy Mini Wireless and the simulator through one gateway.

## Components

Each adapter serves one robot under its engine id, for example `reachy_sim/reaction`. All
four components are canonical RoIS types.

| Component | Ref | On the Reachy Mini |
|-----------|-----|--------------------|
| SystemInformation | `<engine id>/system_information` | The status of the robot, and the yaw of its body as its position |
| Reaction | `<engine id>/reaction` | Expressions of the head and the antennas: `neutral`, `nod`, `shake`, `happy`, `sad` and `curious`, and Pollen's recorded emotions with `--emotions` |
| SpeechSynthesis | `<engine id>/speech_synthesis` | A text spoken on the speaker with a Piper voice, or with a voice-like tone without one, while the head moves to the voice |
| PersonDetection | `<engine id>/person_detection` | `person_detected` with the number of faces the camera sees, each time that number changes |

The four components share one connection to the robot's daemon. Speech and a reaction
run together: the head moves to the voice on top of the expression. A reaction or a speech
that starts while the previous one runs cuts it off, and the previous one ends with ABORT.

## Install

The adapter needs Python 3.12, the version that both the OpenRoIS packages and the
Reachy Mini SDK support, and the Reachy Mini SDK at the version of the robot's daemon, 1.9
or later. Until their first release, the OpenRoIS packages come from the openrois
repository at a pinned commit.

```bash
uv venv --python 3.12 ~/venvs/openrois-reachy
# The SDK at the daemon's version, with the simulator.
uv pip install --python ~/venvs/openrois-reachy "reachy-mini[mujoco]==1.11.0"
uv pip install --python ~/venvs/openrois-reachy -e .
```

### On Linux

The SDK and the daemon play sound and read the camera with GStreamer. On Ubuntu 24.04:

```bash
sudo apt install gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
  libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev libgstreamer-plugins-bad1.0-dev \
  libssl-dev pkg-config \
  gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-alsa \
  gstreamer1.0-pulseaudio gstreamer1.0-nice gstreamer1.0-tools
```

They also need the WebRTC plugin of
[gst-plugins-rs](https://gitlab.freedesktop.org/gstreamer/gst-plugins-rs), which Ubuntu
24.04 does not package. With a Rust toolchain:

```bash
cargo install cargo-c
git clone --depth 1 --branch 0.14.5 https://gitlab.freedesktop.org/gstreamer/gst-plugins-rs.git
cd gst-plugins-rs
cargo cinstall -p gst-plugin-webrtc --prefix="$HOME/.local/gst-plugins-rs"
mkdir -p ~/.local/share/gstreamer-1.0/plugins
ln -s "$(find "$HOME/.local/gst-plugins-rs" -name libgstrswebrtc.so)" ~/.local/share/gstreamer-1.0/plugins/
```

`gst-inspect-1.0 webrtcsrc` then describes the plugin.

## Run

Start an [OpenRoIS gateway](https://github.com/openrois/openrois/tree/dev/gateway) first,
on `ws://127.0.0.1:8765` by default. Then start one adapter per robot:

```bash
# The simulator on this machine.
reachy-mini-daemon --sim
openrois-adapter-reachy-mini --engine-id reachy_sim --sim

# The Reachy Mini Wireless on the network.
openrois-adapter-reachy-mini --engine-id reachy_real --host reachy-mini.local
```

The simulator renders the camera only with its viewer open, so with `--headless`
PersonDetection reports nothing. Under Wayland the MuJoCo viewer runs through XWayland:

```bash
env -u WAYLAND_DISPLAY GDK_BACKEND=x11 reachy-mini-daemon --sim
```

An HRI client then connects to the gateway and finds both robots in its profile, for
example the [HRI client](https://github.com/openrois/openrois/tree/dev/examples/hri-client)
of the openrois repository on `ws://localhost:8765`. A client binds the Reaction and the
SpeechSynthesis of a robot before it sets their parameters or starts them.

### Speech With Piper

Without voices, the robot speaks with a voice-like tone. For real speech, install
[Piper](https://github.com/OHF-Voice/piper1-gpl) in its own environment and download a
voice. Piper is GPL-3.0-or-later, so the adapter runs the `piper` command and does not
depend on it.

```bash
uv venv --python 3.12 ~/venvs/piper
uv pip install --python ~/venvs/piper piper-tts
mkdir -p ~/piper-voices
~/venvs/piper/bin/python -m piper.download_voices en_US-lessac-medium --data-dir ~/piper-voices
openrois-adapter-reachy-mini --engine-id reachy_sim --sim \
  --piper ~/venvs/piper/bin/piper --piper-voices ~/piper-voices
```

Each voice in the folder is a `character` of SpeechSynthesis, and its name starts with
its `language`, for example `en` for `en_US-lessac-medium`. `default` is the first voice
of the language.

### Settings

Every flag falls back to an environment variable.

| Flag | Environment | Default | Setting |
|------|-------------|---------|---------|
| `--gateway-url` | `OPENROIS_GATEWAY_URL` | `ws://127.0.0.1:8765` | The gateway to connect to |
| `--engine-id` | `OPENROIS_ENGINE_ID` | `reachy_mini` | The engine id, the first part of every ref |
| `--host` | `OPENROIS_REACHY_HOST` | `reachy-mini.local` | The host of a robot on the network |
| `--connection-mode` | `OPENROIS_REACHY_CONNECTION_MODE` | `auto` | `localhost_only`, `network`, or `auto` to try this machine first |
| `--sim` | | | The simulator on this machine, `--connection-mode localhost_only` |
| `--emotions` | `OPENROIS_REACHY_EMOTIONS` | off | Add Pollen's recorded emotions to the reactions, from the Hugging Face Hub |
| `--piper` | `OPENROIS_PIPER` | `piper` | The command that runs Piper |
| `--piper-voices` | `OPENROIS_PIPER_VOICES` | none | The folder of the Piper voices |
| `--log-level` | | `info` | `debug`, `info`, `warning` or `error` |

The adapter reconnects to the gateway whenever the connection drops. Ctrl+C stops it.

## Layout

- `src/openrois/components/reachy_mini/`: the components, imported as
  `openrois.components.reachy_mini`. Once validated, they move to the openrois repository
  as `openrois-components-reachy-mini`, under the same import path.
- `src/openrois_adapter_reachy_mini/`: the adapter command, one process per robot.
- `scripts/spike.py`: checks how the installed Reachy Mini SDK behaves on the robot or
  the simulator, and writes a JSON report.

## Development

The tests run without the SDK and without a robot: a stand-in SDK plays both robots.

```bash
python -m pip install -e ".[dev]"
ruff check src tests scripts
mypy
pytest
```

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
