# OpenRoIS Adapter for Reachy Mini

The [OpenRoIS](https://openrois.org) adapter for the Pollen Robotics
[Reachy Mini](https://huggingface.co/docs/reachy_mini). It serves canonical RoIS components
for a Reachy Mini, the real robot or its simulator, so that a service application drives
the robot through an OpenRoIS gateway with the standard RoIS interfaces.

## RoIS in Two Minutes

RoIS, the OMG Robotic Interaction Service framework, is a standard way for an
application to use a robot's human-robot interaction functions without knowing which
robot it talks to. The robot offers **components** of standard types, such as Reaction
or SpeechSynthesis. An application works with them in a fixed order:

1. **connect** to the gateway, and read its **profile** to see the robots and components.
2. **search** for a component of a type, and **bind** it, which reserves it for the
   application. Only the application that holds the bind of the Reaction moves the head.
3. **set_parameter** to configure a component, for example the text to speak, and
   **execute** commands such as `start` and `stop`. Each command completes with a status:
   OK, ERROR, ABORT when it was stopped, or TIMEOUT.
4. **subscribe** to events, such as the number of persons in view, and **query** values,
   such as the voices the robot has.
5. **release** and **disconnect** when done.

When something goes wrong, for example the robot's daemon stops, the application gets a
**notify_error** and asks **get_error_detail** what happened. The gateway speaks JSON-RPC
over WebSocket, and the openrois repository has client SDKs for JavaScript, Python and
Unity.

This adapter is the robot's side: one process per robot that connects to a gateway and
serves the robot's four components.

## Status

Version 0.1.0, alpha, pre-1.0, unstable API. The four components and the adapter command
run on the released `openrois-engine`, `openrois-components-core` and
`openrois-interfaces`. The whole workflow below passes against Pollen's simulator, and the
adapter has driven a Reachy Mini Wireless with daemon 1.14.0 over Wi-Fi. The settings and
the behaviour may still change before 1.0.

## Components

Each adapter serves one robot under its engine id, for example `reachy_sim/reaction`. All
four components are canonical RoIS types.

| Component | Ref | On the Reachy Mini |
|-----------|-----|--------------------|
| SystemInformation | `<engine id>/system_information` | `robot_position`, the yaw of the body, and `engine_status` |
| Reaction | `<engine id>/reaction` | `start` plays the expression named by `reaction_ref`: `neutral`, `nod` (the default), `shake`, `happy`, `sad` or `curious`, or one of Pollen's recorded emotions with `reaction.emotions`. `available_reactions` lists them |
| SpeechSynthesis | `<engine id>/speech_synthesis` | `start` speaks `speech_text` on the speaker with a Piper voice, or with a voice-like tone without one, while the head moves to the voice |
| PersonDetection | `<engine id>/person_detection` | From `start` to `stop`, `person_detected` with the number of faces the camera sees, on each start and each time that number changes. `suspend` holds the events back and `resume` sends them again |

Reaction and SpeechSynthesis are actuation components: one application binds each at a
time, and a second one gets OUT_OF_RESOURCES. So the workflow script below and an agent
that drives the robot through the OpenRoIS MCP server cannot hold them at the same time:
while the MCP server holds a bind, the workflow stops at `bind` and names the component.
Release it from the agent first, quit the session that runs the MCP server, or start the
MCP server with `--idle-release SECONDS` so that it lets idle binds go.
PersonDetection and SystemInformation are shared, so several applications bind and use
them together.

Speech and a reaction run together: the head moves to the voice on top of the
expression. `stop`, or a new `start` while the previous one runs, cuts it off, and the
previous one ends with ABORT. `suspend` on Reaction or SpeechSynthesis holds the next
`start` until `resume`. A command a component does not offer is answered UNSUPPORTED.

The four components share one connection to the robot's daemon. While the daemon does
not answer, every component reports ERROR in `component_status`, the applications that
bound or subscribed to them get `notify_error`, and their commands and queries are
refused. Once the daemon is back, the components report READY again. A state stream that
only pauses for a moment, as over Wi-Fi, raises no error and does not reconnect, though a
command that runs during the pause can fail: the daemon counts as lost once the pause has
lasted `connection.lost_after` seconds.

The camera and the speaker can fail while the daemon answers, for example while its
media server restarts or another application has released it. Then only PersonDetection
and SpeechSynthesis report ERROR, with the reason, while the robot still moves, and they
report READY again once the media work. A speech that the robot's daemon refuses to play
ends with ERROR rather than OK.

## Install

The adapter needs Python 3.12, the version that both the OpenRoIS packages and the
Reachy Mini SDK support, and the Reachy Mini SDK at the version of the robot's daemon, 1.9
or later. Read the daemon's version first, the `version` field of its status:

```bash
curl http://reachy-mini.local:8000/api/daemon/status
```

Then install the adapter, the SDK at that version, 1.14.0 here, with the simulator, and
the gateway:

```bash
uv venv --python 3.12 ~/venvs/openrois-reachy
uv pip install --python ~/venvs/openrois-reachy openrois-adapter-reachy-mini \
  "reachy-mini[mujoco]==1.14.0" openrois-gateway
```

The `robot` extra, `openrois-adapter-reachy-mini[robot]`, installs the latest SDK
instead, which suits a robot that runs the latest daemon. An SDK of another version
than the daemon works, with a warning.

### On Linux

The SDK and the daemon play sound and read the camera with GStreamer, and on Linux the
SDK builds PyGObject and pycairo from source, which needs a compiler and their headers.
On Ubuntu 24.04:

```bash
sudo apt install build-essential pkg-config libgirepository1.0-dev libcairo2-dev \
  gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
  libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev libgstreamer-plugins-bad1.0-dev \
  libssl-dev \
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

Start an [OpenRoIS gateway](https://pypi.org/project/openrois-gateway/) first,
`openrois-gateway`, on `ws://127.0.0.1:8765` by default. Then start one adapter per robot,
with the settings files of the [next section](#the-settings-files). Each command runs
until Ctrl+C, so give each its own terminal, where the environment is active:

```bash
source ~/venvs/openrois-reachy/bin/activate

# The gateway.
openrois-gateway

# The simulator on this machine.
reachy-mini-daemon --sim
openrois-adapter-reachy-mini --config adapter.sim.yaml

# The Reachy Mini Wireless on the network, at its IP address.
openrois-adapter-reachy-mini --config adapter.yaml --robot-address 192.168.1.42
```

The adapter reaches the robot at `robot_address` over the network, and the daemon on
this machine with `simulator: true`, so the two never mix up when the simulator runs
next to a real robot. It checks the daemon every three seconds and connects again once a
daemon that stopped or dropped off the network is back. The adapter and the daemon start
in any order.

`robot_address` is best the robot's IP address, which the Reachy Mini app or your
router shows. A `.local` name such as `reachy-mini.local` works too, as long as name
resolution on your network does, which can fail now and then. Reachy Mini daemons 1.12
and later refuse any other name, such as `reachy-mini` or a name your router hands out:
the daemon then looks as if it does not answer, and the adapter warns at start.

With the simulator and a real robot on the same machine, check before a demo that the
robot's daemon reports its Wi-Fi address: `curl http://192.168.1.42:8000/api/daemon/status`
must show a `wlan_ip` equal to the robot's address. A daemon that started before its
Wi-Fi had an address reports none, and the SDK would then take the camera and the
speaker of this machine, the simulator's. The adapter refuses that, and PersonDetection
and SpeechSynthesis of the robot report ERROR until the robot's daemon restarts with
`curl -X POST http://192.168.1.42:8000/api/daemon/restart`.

### The Settings Files

The settings come from a YAML file, named by `--config`. The repository and the source
distribution ship two, and after a `pip install` you write them yourself. For a Reachy
Mini Wireless, `adapter.yaml`:

```yaml
# Settings of the Reachy Mini adapter for a Reachy Mini Wireless on the network.
# OPENROIS_ADAPTER_* environment variables and flags override the four common settings at
# the top. The sections below them belong to the connection and the components. See
# README.md for every setting.

gateway_url: ws://127.0.0.1:8765
engine_id: reachy_real
# The robot's IP address is the most reliable. A .local name works too, and the daemon
# refuses any other name.
robot_address: reachy-mini.local
log_level: info

simulator: false
time_scale: 1.0

connection:
  retry: 3.0

reaction:
  emotions: false

speech_synthesis:
  piper: piper
  # piper_voices: ~/piper-voices
```

For the simulator on this machine, `adapter.sim.yaml`:

```yaml
# Settings of the Reachy Mini adapter for Pollen's simulator on this machine, started with
# reachy-mini-daemon --sim. Select it with --config adapter.sim.yaml.

gateway_url: ws://127.0.0.1:8765
engine_id: reachy_sim
log_level: info

simulator: true
time_scale: 1.0

reaction:
  emotions: false
```

Without a file the engine id is `reachy_mini` and the adapter reaches the robot over the
network, so the workflow below then needs `--engine-id reachy_mini`. The simulator can
only be chosen in a file.

### PersonDetection's Model

PersonDetection counts faces with the SDK's face detector, whose model comes from the
Hugging Face Hub the first time and from the cache after. Without the Hub and without a
cached model, for example on the robot's own hotspot, PersonDetection reports ERROR and
tries again until it gets the model. While you have internet access, fill the cache once
with the adapter's environment, and again after an SDK upgrade:

```bash
~/venvs/openrois-reachy/bin/python -c \
  "from reachy_mini.vision.face_detector import FaceDetector; FaceDetector()"
```

A count is reported once it holds for two seconds, and a face that the edge of the frame
cuts off does not count, since its score comes and goes as the person moves.

The simulator renders the camera only with its viewer open, so with `--headless`
PersonDetection reports nothing. Under Wayland the MuJoCo viewer runs through XWayland:

```bash
env -u WAYLAND_DISPLAY GDK_BACKEND=x11 reachy-mini-daemon --sim
```

An HRI client then connects to the gateway and finds both robots in its profile, for
example the [OpenRoIS Inspector](https://github.com/openrois/openrois/tree/main/apps/inspector)
of the openrois repository on `ws://localhost:8765`.

### Check That It Works

The adapter ships the whole RoIS workflow as a script. With the gateway, the daemon and
the adapter running, it connects as an application, finds the robot's components, binds
them, makes the robot speak while it plays a reaction, waits for a person count, stops
the daemon to see the error reach it, starts the daemon again, and disconnects:

```bash
python -m openrois_adapter_reachy_mini.workflow --engine-id reachy_sim

# The Reachy Mini Wireless.
python -m openrois_adapter_reachy_mini.workflow --engine-id reachy_real \
  --daemon http://reachy-mini.local:8000
```

It prints the daemon's media status, then each step as it passes, and stops at the
first one that fails. A completed speech only says that the daemon took the sound, so
after the speech the workflow asks whether you heard the robot. `--yes` skips the
question. It stops and starts the daemon through the daemon's HTTP API, and leaves the
robot where it is. Run the simulator with its viewer, since the person count needs the
camera. While another application, such as the MCP server of an agent, holds the bind of
Reaction or SpeechSynthesis, the workflow stops at `bind`: see
[Components](#components).

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
```

Then name Piper and the voices in the `speech_synthesis` section of `adapter.yaml`:

```yaml
speech_synthesis:
  piper: ~/venvs/piper/bin/piper
  piper_voices: ~/piper-voices
```

Each voice in the folder is a `character` of SpeechSynthesis, and its name starts with
its `language`, for example `en` for `en_US-lessac-medium`. The default `character` is
the first voice of `available_voices`, `tone` without Piper, and until a client picks
another voice a start speaks with the first voice of `language`. The adapter refuses to
start when `piper` is not a program it can run.

`volume` is the share of the voice's full level, from 0 to 100, and 100 by default. Piper
writes every sentence at full scale, so a louder voice would only distort it: for a
louder robot, raise the robot's own volume.

### Settings

The settings come from `adapter.yaml`, named by `--config` or `OPENROIS_ADAPTER_CONFIG`.
The four settings at the top can also come from the environment or a flag, which win
over the file:

| Setting | Flag | Environment | Default | What it is |
|---------|------|-------------|---------|------------|
| `gateway_url` | `--gateway-url` | `OPENROIS_ADAPTER_GATEWAY_URL` | `ws://127.0.0.1:8765` | The gateway to connect to |
| `engine_id` | `--engine-id` | `OPENROIS_ADAPTER_ENGINE_ID` | `reachy_mini` | The engine id, the first part of every ref |
| `robot_address` | `--robot-address` | `OPENROIS_ADAPTER_ROBOT_ADDRESS` | `reachy-mini.local` | The host of the robot's daemon on the network: an IP address, or a `.local` name |
| `log_level` | `--log-level` | `OPENROIS_ADAPTER_LOG_LEVEL` | `info` | `debug`, `info`, `warning` or `error` |

The others are only in the file:

| Setting | Default | What it is |
|---------|---------|------------|
| `simulator` | `false` | `true` for the daemon on this machine, such as the simulator, instead of `robot_address` |
| `time_scale` | `1.0` | Factor on every duration the adapter times itself: the reactions and their blends, the daemon checks and `lost_after`, the PersonDetection frames and hold, and the wait after a speech. Below 1 runs faster |
| `connection.port` | `8000` | The port of the daemon |
| `connection.timeout` | `5.0` | Seconds to wait for the daemon |
| `connection.retry` | `3.0` | Seconds between two checks of the daemon |
| `connection.lost_after` | `3.0` | Seconds the daemon's state stream may pause, its connection open, before the adapter connects again |
| `reaction.emotions` | `false` | Add Pollen's recorded emotions to the reactions, from the Hugging Face Hub |
| `reaction.blend` | `0.3` | Seconds a reaction eases from the current pose into its first pose |
| `speech_synthesis.piper` | `piper` | The command that runs Piper. `~` stands for the home folder |
| `speech_synthesis.piper_voices` | none | The folder of the Piper voices. Without it the robot speaks with a tone |
| `speech_synthesis.wobble` | `true` | Whether the head moves with the voice |
| `person_detection.interval` | `0.2` | Seconds between two camera frames |
| `person_detection.hold` | `2.0` | Seconds a new count holds before it is reported |
| `person_detection.threshold` | `0.6` | The detection score from which a face counts, from 0 to 1 |
| `person_detection.ignore_edge_faces` | `true` | Leave faces that the edge of the frame cuts off out of the count |

A setting the adapter does not know, or a value out of range, stops it with a message
that names the setting.

The adapter reconnects to the gateway whenever the connection drops. Ctrl+C stops it.

## Layout

- `src/openrois/components/reachy_mini/`: the components, imported as
  `openrois.components.reachy_mini`.
- `src/openrois_adapter_reachy_mini/`: the adapter command, one process per robot, and
  `workflow`, the end-to-end check.
- `scripts/spike.py`: checks how the installed Reachy Mini SDK behaves on the robot or
  the simulator, and writes a JSON report.

## Development

The tests run without the SDK and without a robot: a stand-in SDK plays both robots.
From a clone of the repository:

```bash
python -m pip install -e ".[dev]"   # The OpenRoIS packages and the gateway, for the tests
ruff check .
ruff format --check .
mypy
pytest
```

A tag such as `v0.1.0-alpha.1` releases the version of `pyproject.toml`, 0.1.0a1, to
PyPI, and then creates the GitHub release with its section of the changelog.

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
