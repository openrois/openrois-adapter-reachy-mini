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
over WebSocket, and the openrois repository has clients for Python and TypeScript.

This adapter is the robot's side: one process per robot that connects to a gateway and
serves the robot's four components.

## Status

In progress. The four components and the adapter command run on the OpenRoIS engine of
the first release, and the whole workflow below passes against Pollen's simulator. The
first release, 0.1.0a1, comes together with the first releases of `openrois-engine`,
`openrois-components-core` and `openrois-gateway`, once the same workflow has passed on a
Reachy Mini Wireless.

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
time, and a second one gets OUT_OF_RESOURCES. PersonDetection and SystemInformation are
shared, so several applications bind and use them together. Speech and a reaction run
together: the head moves to the voice on top of the expression. `stop`, or a new `start`
while the previous one runs, cuts it off, and the previous one ends with ABORT.
`suspend` on Reaction or SpeechSynthesis holds the next `start` until `resume`. A command
a component does not offer is answered UNSUPPORTED.

The four components share one connection to the robot's daemon. While the daemon does
not answer, every component reports ERROR in `component_status`, the applications that
bound or subscribed to them get `notify_error`, and their commands and queries are
refused. Once the daemon is back, the components report READY again.

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
on `ws://127.0.0.1:8765` by default. Then start one adapter per robot, in a terminal
where the environment is active:

```bash
source ~/venvs/openrois-reachy/bin/activate

# The simulator on this machine.
reachy-mini-daemon --sim
openrois-adapter-reachy-mini --config adapter.sim.yaml

# The Reachy Mini Wireless on the network.
openrois-adapter-reachy-mini --config adapter.yaml --robot-address reachy-mini.local
```

The adapter reaches the robot at `robot_address` over the network, and the daemon on
this machine with `simulator: true`, so the two never mix up when the simulator runs
next to a real robot. It checks the daemon every three seconds and connects again once a
daemon that stopped or dropped off the network is back. The adapter and the daemon start
in any order.

The simulator renders the camera only with its viewer open, so with `--headless`
PersonDetection reports nothing. Under Wayland the MuJoCo viewer runs through XWayland:

```bash
env -u WAYLAND_DISPLAY GDK_BACKEND=x11 reachy-mini-daemon --sim
```

An HRI client then connects to the gateway and finds both robots in its profile, for
example the [HRI client](https://github.com/openrois/openrois/tree/dev/examples/hri-client)
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

It prints each step as it passes, and stops at the first one that fails. It stops and
starts the daemon through the daemon's HTTP API, and leaves the robot where it is. Run
the simulator with its viewer, since the person count needs the camera.

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
its `language`, for example `en` for `en_US-lessac-medium`. `default` is the first voice
of the language.

### Settings

The settings moved from flags to `adapter.yaml` in this version: `--sim` is now
`simulator: true` in the file, and `--host` is `robot_address`. The changelog lists the
flags and variables that are gone.

The settings come from `adapter.yaml`, named by `--config` or `OPENROIS_ADAPTER_CONFIG`.
The repository has two: `adapter.yaml` for a Reachy Mini Wireless and
`adapter.sim.yaml` for the simulator. The four settings at the top can also come from
the environment or a flag, which win over the file:

| Setting | Flag | Environment | Default | What it is |
|---------|------|-------------|---------|------------|
| `gateway_url` | `--gateway-url` | `OPENROIS_ADAPTER_GATEWAY_URL` | `ws://127.0.0.1:8765` | The gateway to connect to |
| `engine_id` | `--engine-id` | `OPENROIS_ADAPTER_ENGINE_ID` | `reachy_mini` | The engine id, the first part of every ref |
| `robot_address` | `--robot-address` | `OPENROIS_ADAPTER_ROBOT_ADDRESS` | `reachy-mini.local` | The host of the robot's daemon on the network |
| `log_level` | `--log-level` | `OPENROIS_ADAPTER_LOG_LEVEL` | `info` | `debug`, `info`, `warning` or `error` |

The others are only in the file:

| Setting | Default | What it is |
|---------|---------|------------|
| `simulator` | `false` | `true` for the daemon on this machine, such as the simulator, instead of `robot_address` |
| `time_scale` | `1.0` | Factor on every duration the adapter times itself: the reactions and their blends, the daemon checks, the PersonDetection frames and hold, and the wait after a speech. Below 1 runs faster |
| `connection.port` | `8000` | The port of the daemon |
| `connection.timeout` | `5.0` | Seconds to wait for the daemon |
| `connection.retry` | `3.0` | Seconds between two checks of the daemon |
| `reaction.emotions` | `false` | Add Pollen's recorded emotions to the reactions, from the Hugging Face Hub |
| `reaction.blend` | `0.3` | Seconds a reaction eases from the current pose into its first pose |
| `speech_synthesis.piper` | `piper` | The command that runs Piper |
| `speech_synthesis.piper_voices` | none | The folder of the Piper voices. Without it the robot speaks with a tone |
| `speech_synthesis.wobble` | `true` | Whether the head moves with the voice |
| `person_detection.interval` | `0.2` | Seconds between two camera frames |
| `person_detection.hold` | `0.5` | Seconds a new count holds before it is reported |
| `person_detection.threshold` | `0.6` | The detection score from which a face counts, from 0 to 1 |

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

```bash
python -m pip install -e ".[dev]"   # The OpenRoIS packages and the gateway, for the tests
ruff check src tests scripts
mypy
pytest
```

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
