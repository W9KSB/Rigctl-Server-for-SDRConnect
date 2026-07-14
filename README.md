# SDRConnect NetControl

SDRConnect NetControl is a single-file Python middleware bridge that lets
`rigctl` or other Hamlib-compatible network clients control the frequency tuned
in SDRConnect.

It listens like a basic `rigctld` network server, receives Hamlib-style
frequency commands, and forwards them to SDRConnect through SDRConnect's local
WebSocket API.

## What It Does

- Receives rigctl/Hamlib TCP commands on port `4532`
- Exposes a small HTTP control API on port `4545`
- Sends frequency, mode, and related control commands to SDRConnect over
  `127.0.0.1:5454`
- Keeps frequency state bidirectional by listening for SDRConnect WebSocket
  updates
- Allows LAN clients to control SDRConnect without exposing SDRConnect's
  WebSocket API to the network
- Runs on Windows and Linux with Python 3
- Uses only the Python standard library

## Requirements

- Python 3.10 or newer
- SDRConnect 1.0.6 or newer
- SDRConnect WebSocket/API support enabled in SDRConnect
- `rigctl` or another Hamlib-compatible client

No Python packages need to be installed.

## SDRConnect Setup

1. Open SDRConnect.
2. Enable the WebSocket/API/module option in SDRConnect.
3. Keep SDRConnect running.
4. Confirm the local API is listening.

On Windows PowerShell:

```powershell
Test-NetConnection 127.0.0.1 -Port 5454
```

You want:

```text
TcpTestSucceeded : True
```

If that test fails, SDRConnect NetControl cannot talk to SDRConnect yet.

## Running On Windows

Open PowerShell in the project folder:

```powershell
cd path\to\SDRConnect-NetControl
```

Run the bridge:

```powershell
python .\sdrconnect_netcontrol.py
```

For debug logging:

```powershell
python .\sdrconnect_netcontrol.py --verbose
```

If your Windows install uses the Python launcher, this also works:

```powershell
py .\sdrconnect_netcontrol.py --verbose
```

## Running On Linux

From the project folder:

```bash
python3 ./sdrconnect_netcontrol.py
```

For debug logging:

```bash
python3 ./sdrconnect_netcontrol.py --verbose
```

## Client Setup

Run SDRConnect NetControl on the same machine as SDRConnect.

Configure your Hamlib-compatible client to connect to the machine running
SDRConnect NetControl:

- Host: the Windows/Linux machine's LAN IP address
- Port: `4532`
- Protocol: Hamlib/rigctl network control

Example:

```text
Host: the machine running SDRConnect NetControl
Port: 4532
```

SDRConnect NetControl listens on `0.0.0.0:4532` by default, so clients can run
on the same machine or another machine on the LAN.

## HTTP Control API

SDRConnect NetControl also listens on `0.0.0.0:4545` by default for a minimal
HTTP API.

Supported routes:

- `POST /api/start-audio-recording`
- `POST /api/stop-audio-recording`

`POST /api/start-audio-recording` sends the SDRConnect commands needed to:

1. unmute audio playback
2. start the selected device stream
3. start audio recording

`POST /api/stop-audio-recording` sends the SDRConnect commands needed to:

1. stop recording
2. stop the selected device stream

No request body is required.

Example PowerShell calls:

```powershell
Invoke-WebRequest -Method Post http://127.0.0.1:4545/api/start-audio-recording
Invoke-WebRequest -Method Post http://127.0.0.1:4545/api/stop-audio-recording
```

## Command Line Options

### `--listen-host ADDRESS` (default: `0.0.0.0`)

Address for the rigctl/Hamlib TCP listener.

### `--listen-port PORT` (default: `4532`)

Port for the rigctl/Hamlib TCP listener.

### `--api-listen-host ADDRESS` (default: `0.0.0.0`)

Address for the HTTP control API listener.

### `--api-listen-port PORT` (default: `4545`)

Port for the HTTP control API listener. Use `0` to disable it.

### `--sdr-port PORT` (default: `5454`)

Local SDRConnect WebSocket API port.

### `--device primary|secondary` (default: `primary`)

SDRConnect device target.

### `--frequency-property device_vfo_frequency|device_center_frequency` (default: `device_vfo_frequency`)

SDRConnect frequency property to control.

### `--request-timeout SECONDS` (default: `1.5`)

How long to wait for SDRConnect `get_property` responses.

### `--verbose`

Enable debug logging, including rigctl commands and SDRConnect WebSocket JSON
traffic.

## Examples

Run with defaults:

```bash
python3 ./sdrconnect_netcontrol.py
```

Run with verbose logging:

```bash
python3 ./sdrconnect_netcontrol.py --verbose
```

Disable the HTTP control API:

```bash
python3 ./sdrconnect_netcontrol.py --api-listen-port 0
```

Use a different rigctl listener port:

```bash
python3 ./sdrconnect_netcontrol.py --listen-port 4533
```

Control SDRConnect center frequency instead of VFO frequency:

```bash
python3 ./sdrconnect_netcontrol.py --frequency-property device_center_frequency
```

## How It Works

SDRConnect NetControl has two sides.

The Hamlib side listens for TCP clients on port `4532`. A compatible client
connects to that listener and sends rigctl-style commands such as setting or
reading the current frequency.

The HTTP side listens for POST requests on port `4545`. It exposes two
purpose-built endpoints for starting and stopping audio recording without
requiring callers to know the underlying SDRConnect WebSocket messages.

The SDRConnect side connects to SDRConnect's local WebSocket API at
`127.0.0.1:5454`. It sends SDRConnect `set_property` and `get_property` JSON
messages to control the tuned frequency and read the current state, plus
`device_stream_enable`, `start_recording`, and `stop_recording` events for the
HTTP recording API.

SDRConnect's WebSocket API is intentionally accessed only through localhost.
That keeps the SDRConnect API off the LAN while still allowing compatible
clients to connect to the middleware over the network.

## Supported Rigctl Commands

The bridge supports the common commands needed for satellite tracking and basic
radio control:

- `F` / `set_freq`
- `f` / `get_freq`
- `M` / `set_mode`
- `m` / `get_mode`
- `V` / `set_vfo`
- `v` / `get_vfo`
- `T` / `set_ptt`
- `t` / `get_ptt`
- `dump_caps`
- `get_info`
- `chk_vfo`
- `q` / `Q`

Frequency control is the main intended use.

## Troubleshooting

### SDRConnect connection refused

Check that SDRConnect's WebSocket/API option is enabled and listening:

```powershell
Test-NetConnection 127.0.0.1 -Port 5454
```

If `TcpTestSucceeded` is `False`, fix SDRConnect first.

### Client cannot connect

Make sure SDRConnect NetControl is running and listening on port `4532`.

On Windows:

```powershell
netstat -ano -p tcp | findstr :4532
```

If the client is on another machine, Windows Firewall may need an inbound rule
allowing TCP port `4532` on private networks.

### HTTP API client cannot connect

Make sure SDRConnect NetControl is listening on port `4545`.

On Windows:

```powershell
netstat -ano -p tcp | findstr :4545
```

If the caller is on another machine, Windows Firewall may need an inbound rule
allowing TCP port `4545` on private networks.

### Need more detail

Run with verbose logging:

```powershell
python .\sdrconnect_netcontrol.py --verbose
```

This prints rigctl commands and SDRConnect WebSocket messages.

## Converting To A Windows Executable

The easiest way to make a standalone Windows `.exe` is PyInstaller.

Install PyInstaller:

```powershell
python -m pip install pyinstaller
```

Build a single-file executable:

```powershell
python -m PyInstaller --onefile --name SDRConnect-NetControl .\sdrconnect_netcontrol.py
```

The executable will be created at:

```text
dist\SDRConnect-NetControl.exe
```

Run it:

```powershell
.\dist\SDRConnect-NetControl.exe --verbose
```

The executable still needs SDRConnect running on the same machine with the
WebSocket/API option enabled.
