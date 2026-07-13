#!/usr/bin/env python3
"""Hamlib rigctld-style TCP bridge for SDRConnect WebSocket control.

Typical use:
  python sdrconnect_netcontrol.py

Defaults:
  - listens for rigctl/Hamlib clients on 0.0.0.0:4532 for LAN access
  - connects to SDRConnect WebSocket on 127.0.0.1:5454
  - controls SDRConnect's device_vfo_frequency property

Useful options:
  --frequency-property device_center_frequency
  --refresh-interval 0.25
  --listen-port 4533
  --verbose

Supported rigctl commands include F/f, set_freq/get_freq, M/m,
set_mode/get_mode, V/v, set_vfo/get_vfo, T/t, set_ptt/get_ptt,
dump_caps, get_info, chk_vfo, and q/Q.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import contextlib
import os
import json
import logging
import signal
import struct
from dataclasses import dataclass
from typing import Awaitable, Callable


HAMLIB_OK = "RPRT 0\n"
HAMLIB_ERR_INVALID_PARAM = "RPRT -1\n"
HAMLIB_ERR_NOT_IMPLEMENTED = "RPRT -4\n"

SDR_MODES = {"AM", "USB", "LSB", "CW", "SAM", "NFM", "WFM"}
HAMLIB_TO_SDR_MODE = {
    "AM": "AM",
    "USB": "USB",
    "LSB": "LSB",
    "CW": "CW",
    "CWR": "CW",
    "SAM": "SAM",
    "FM": "NFM",
    "NFM": "NFM",
    "WFM": "WFM",
}


@dataclass
class BridgeState:
    frequency_hz: int = 0
    actual_frequency_hz: int = 0
    last_commanded_frequency_hz: int = 0
    mode: str = "NFM"
    passband_hz: int = 15000
    vfo: str = "VFOA"
    ptt: bool = False
    connected: bool = False
    started: bool = False


class SDRConnectClient:
    def __init__(
        self,
        port: int,
        device: str,
        frequency_property: str,
        state: BridgeState,
        request_timeout: float,
        refresh_interval: float,
    ) -> None:
        self.host = "127.0.0.1"
        self.port = port
        self.uri = f"ws://{self.host}:{port}/"
        self.device = device
        self.frequency_property = frequency_property
        self.state = state
        self.request_timeout = request_timeout
        self.refresh_interval = refresh_interval
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()
        self._pending_gets: dict[str, asyncio.Future[str]] = {}
        self._stop = asyncio.Event()

    async def run(self) -> None:
        while not self._stop.is_set():
            refresh_task: asyncio.Task[None] | None = None
            try:
                logging.info("Connecting to SDRConnect at %s", self.uri)
                self._reader, self._writer = await self._connect()
                self.state.connected = True
                logging.info("Connected to SDRConnect")
                for property_name in ("started", self.frequency_property, "demodulator", "filter_bandwidth"):
                    with contextlib.suppress(Exception):
                        await self.request_property(property_name)
                if self.refresh_interval > 0:
                    refresh_task = asyncio.create_task(self._refresh_frequency_cache())
                while not self._stop.is_set():
                    opcode, payload = await self._read_frame()
                    if opcode == 0x1:
                        self._handle_message(payload.decode("utf-8", errors="replace"))
                    elif opcode == 0x8:
                        break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logging.warning("SDRConnect connection failed: %s", exc)
            finally:
                if refresh_task is not None:
                    refresh_task.cancel()
                    with contextlib.suppress(Exception):
                        await refresh_task
                self.state.connected = False
                if self._writer is not None:
                    self._writer.close()
                    with contextlib.suppress(Exception):
                        await self._writer.wait_closed()
                self._reader = None
                self._writer = None
                for future in self._pending_gets.values():
                    if not future.done():
                        future.set_exception(ConnectionError("SDRConnect disconnected"))
                self._pending_gets.clear()
            await asyncio.sleep(2)

    def stop(self) -> None:
        self._stop.set()

    async def _refresh_frequency_cache(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(self.refresh_interval)
            if not self.state.connected:
                continue
            try:
                await self.request_property("started")
                if self.state.started:
                    await self.request_property(self.frequency_property)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logging.debug("Frequency cache refresh failed: %s", exc)

    def _handle_message(self, raw: str) -> None:
        logging.debug("SDRConnect recv: %s", raw)
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            logging.debug("Ignoring non-JSON SDRConnect message: %r", raw)
            return

        event_type = message.get("event_type")
        property_name = message.get("property", "")
        value = str(message.get("value", ""))

        if event_type in {"property_changed", "get_property_response"}:
            self._update_cache(property_name, value, source=event_type)

        if event_type == "get_property_response":
            future = self._pending_gets.pop(property_name, None)
            if future and not future.done():
                future.set_result(value)

    def _update_cache(self, property_name: str, value: str, source: str = "local") -> None:
        if property_name == self.frequency_property:
            with contextlib.suppress(ValueError):
                frequency_hz = int(float(value))
                if source == "set_property":
                    self.state.last_commanded_frequency_hz = frequency_hz
                    if frequency_hz != self.state.frequency_hz:
                        self.state.frequency_hz = frequency_hz
                        logging.info(
                            "SDRConnect frequency update (%s): %s Hz",
                            source,
                            frequency_hz,
                        )
                    return

                self.state.actual_frequency_hz = frequency_hz
                if self.state.started and frequency_hz != self.state.frequency_hz:
                    self.state.frequency_hz = frequency_hz
                    logging.info(
                        "SDRConnect frequency update (%s): %s Hz",
                        source,
                        frequency_hz,
                    )
        elif property_name == "started":
            started = value.strip().lower() == "true"
            if started != self.state.started:
                self.state.started = started
                logging.info("SDRConnect started: %s", started)
            if self.state.started:
                if self.state.actual_frequency_hz != self.state.frequency_hz:
                    self.state.frequency_hz = self.state.actual_frequency_hz
                    logging.info(
                        "SDRConnect frequency update (started): %s Hz",
                        self.state.frequency_hz,
                    )
            elif self.state.last_commanded_frequency_hz > 0:
                self.state.frequency_hz = self.state.last_commanded_frequency_hz
        elif property_name == "demodulator" and value:
            self.state.mode = value.upper()
        elif property_name == "filter_bandwidth":
            with contextlib.suppress(ValueError):
                self.state.passband_hz = int(float(value))

    async def send_property(self, property_name: str, value: str | int | float) -> None:
        await self._send(
            {
                "event_type": "set_property",
                "property": property_name,
                "device": self.device,
                "value": str(value),
            }
        )
        self._update_cache(property_name, str(value), source="set_property")

    async def request_property(self, property_name: str) -> str:
        async with self._request_lock:
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            self._pending_gets[property_name] = future
            await self._send(
                {
                    "event_type": "get_property",
                    "property": property_name,
                    "device": self.device,
                    "value": "",
                }
            )
            try:
                return await asyncio.wait_for(future, timeout=self.request_timeout)
            finally:
                self._pending_gets.pop(property_name, None)

    async def _send(self, message: dict[str, str]) -> None:
        async with self._lock:
            if self._writer is None:
                raise ConnectionError("SDRConnect is not connected")
            raw = json.dumps(message, separators=(",", ":"))
            logging.debug("SDRConnect send: %s", raw)
            await self._write_text(raw)

    async def _connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        reader, writer = await asyncio.open_connection(self.host, self.port)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET / HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        writer.write(request.encode("ascii"))
        await writer.drain()

        response = await reader.readuntil(b"\r\n\r\n")
        header_text = response.decode("iso-8859-1")
        status_line, *header_lines = header_text.split("\r\n")
        if " 101 " not in status_line:
            raise ConnectionError(f"WebSocket upgrade failed: {status_line}")
        headers = {}
        for line in header_lines:
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
        ).decode("ascii")
        if headers.get("sec-websocket-accept") != expected:
            raise ConnectionError("WebSocket accept key mismatch")
        return reader, writer

    async def _read_frame(self) -> tuple[int, bytes]:
        if self._reader is None:
            raise ConnectionError("SDRConnect is not connected")
        header = await self._reader.readexactly(2)
        first, second = header
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", await self._reader.readexactly(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", await self._reader.readexactly(8))[0]
        mask = await self._reader.readexactly(4) if masked else b""
        payload = await self._reader.readexactly(length)
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return opcode, payload

    async def _write_text(self, text: str) -> None:
        if self._writer is None:
            raise ConnectionError("SDRConnect is not connected")
        payload = text.encode("utf-8")
        mask = os.urandom(4)
        if len(payload) < 126:
            header = bytes([0x81, 0x80 | len(payload)])
        elif len(payload) <= 0xFFFF:
            header = bytes([0x81, 0x80 | 126]) + struct.pack("!H", len(payload))
        else:
            header = bytes([0x81, 0x80 | 127]) + struct.pack("!Q", len(payload))
        masked_payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self._writer.write(header + mask + masked_payload)
        await self._writer.drain()


class RigctlServer:
    def __init__(
        self,
        listen_host: str,
        listen_port: int,
        state: BridgeState,
        sdr: SDRConnectClient,
    ) -> None:
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.state = state
        self.sdr = sdr
        self._server: asyncio.AbstractServer | None = None

    async def run(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client,
            host=self.listen_host,
            port=self.listen_port,
        )
        sockets = ", ".join(str(sock.getsockname()) for sock in self._server.sockets or [])
        logging.info("Listening for rigctl clients on %s", sockets)
        async with self._server:
            await self._server.serve_forever()

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer = writer.get_extra_info("peername")
        logging.info("rigctl client connected: %s", peer)
        try:
            while not reader.at_eof():
                raw = await reader.readline()
                if not raw:
                    break
                command = raw.decode(errors="replace").strip()
                if not command or command.startswith("#"):
                    continue
                response, should_close = await self._dispatch(command)
                if response:
                    writer.write(response.encode())
                    await writer.drain()
                if should_close:
                    break
        finally:
            writer.close()
            await writer.wait_closed()
            logging.info("rigctl client disconnected: %s", peer)

    async def _dispatch(self, line: str) -> tuple[str, bool]:
        logging.debug("rigctl command: %s", line)
        extended = line.startswith("+")
        if extended:
            line = line[1:].lstrip()

        if line.startswith("\\"):
            line = line[1:]

        parts = line.split()
        if not parts:
            return "", False

        command = parts[0]
        args = parts[1:]
        handler = self._handler_for(command)
        if handler is None:
            logging.debug("Unsupported rigctl command: %s", command)
            return self._format_response(command, HAMLIB_ERR_NOT_IMPLEMENTED, extended), False

        try:
            response, should_close = await handler(args)
        except ValueError:
            response, should_close = HAMLIB_ERR_INVALID_PARAM, False
        except ConnectionError as exc:
            logging.warning("SDRConnect command failed: %s", exc)
            response, should_close = "RPRT -6\n", False

        return self._format_response(command, response, extended), should_close

    def _handler_for(
        self,
        command: str,
    ) -> Callable[[list[str]], Awaitable[tuple[str, bool]]] | None:
        return {
            "F": self._set_freq,
            "set_freq": self._set_freq,
            "f": self._get_freq,
            "get_freq": self._get_freq,
            "M": self._set_mode,
            "set_mode": self._set_mode,
            "m": self._get_mode,
            "get_mode": self._get_mode,
            "V": self._set_vfo,
            "set_vfo": self._set_vfo,
            "v": self._get_vfo,
            "get_vfo": self._get_vfo,
            "T": self._set_ptt,
            "set_ptt": self._set_ptt,
            "t": self._get_ptt,
            "get_ptt": self._get_ptt,
            "q": self._quit,
            "Q": self._quit,
            "chk_vfo": self._chk_vfo,
            "dump_caps": self._dump_caps,
            "1": self._dump_caps,
            "_": self._get_info,
            "get_info": self._get_info,
        }.get(command)

    @staticmethod
    def _format_response(command: str, response: str, extended: bool) -> str:
        if not extended:
            return response
        clean = response.rstrip("\n")
        return f"{command}: {clean}\n"

    async def _set_freq(self, args: list[str]) -> tuple[str, bool]:
        if not args:
            raise ValueError
        frequency = int(float(args[-1]))
        if frequency <= 0:
            raise ValueError
        await self.sdr.send_property(self.sdr.frequency_property, frequency)
        self.state.last_commanded_frequency_hz = frequency
        self.state.frequency_hz = frequency
        return HAMLIB_OK, False

    async def _get_freq(self, args: list[str]) -> tuple[str, bool]:
        return f"{self.state.frequency_hz}\n", False

    async def _set_mode(self, args: list[str]) -> tuple[str, bool]:
        if not args:
            raise ValueError
        mode = args[-2] if len(args) >= 2 and args[-1].lstrip("-").isdigit() else args[-1]
        passband = args[-1] if len(args) >= 2 and args[-1].lstrip("-").isdigit() else None
        if mode == "?":
            return "AM USB LSB CW SAM FM NFM WFM\n", False
        sdr_mode = HAMLIB_TO_SDR_MODE.get(mode.upper())
        if sdr_mode is None or sdr_mode not in SDR_MODES:
            raise ValueError
        await self.sdr.send_property("demodulator", sdr_mode)
        self.state.mode = sdr_mode
        if passband is not None:
            passband_hz = int(passband)
            if passband_hz > 0:
                await self.sdr.send_property("filter_bandwidth", passband_hz)
                self.state.passband_hz = passband_hz
        return HAMLIB_OK, False

    async def _get_mode(self, args: list[str]) -> tuple[str, bool]:
        with contextlib.suppress(Exception):
            mode = await self.sdr.request_property("demodulator")
            self.state.mode = mode.upper()
        with contextlib.suppress(Exception):
            passband = await self.sdr.request_property("filter_bandwidth")
            self.state.passband_hz = int(float(passband))
        return f"{self.state.mode}\n{self.state.passband_hz}\n", False

    async def _set_vfo(self, args: list[str]) -> tuple[str, bool]:
        if not args:
            raise ValueError
        self.state.vfo = args[-1]
        return HAMLIB_OK, False

    async def _get_vfo(self, args: list[str]) -> tuple[str, bool]:
        return f"{self.state.vfo}\n", False

    async def _set_ptt(self, args: list[str]) -> tuple[str, bool]:
        if not args:
            raise ValueError
        self.state.ptt = args[-1] not in {"0", "false", "False", "off", "OFF"}
        return HAMLIB_OK, False

    async def _get_ptt(self, args: list[str]) -> tuple[str, bool]:
        return f"{1 if self.state.ptt else 0}\n", False

    async def _quit(self, args: list[str]) -> tuple[str, bool]:
        return "", True

    async def _chk_vfo(self, args: list[str]) -> tuple[str, bool]:
        return "0\n", False

    async def _get_info(self, args: list[str]) -> tuple[str, bool]:
        status = "connected" if self.state.connected else "disconnected"
        return f"SDRConnect NetControl ({status}, {self.sdr.frequency_property})\n", False

    async def _dump_caps(self, args: list[str]) -> tuple[str, bool]:
        return (
            "Caps dump for model: 2\n"
            "Model name: SDRConnect NetControl\n"
            "Mfg name: SDRplay\n"
            "Backend version: 0.1\n"
            "Can set Frequency: Y\n"
            "Can get Frequency: Y\n"
            "Can set Mode: Y\n"
            "Can get Mode: Y\n"
            "Can set VFO: Y\n"
            "Can get VFO: Y\n"
            "Can get PTT: Y\n"
            "Can set PTT: Y\n"
            "Done\n",
            False,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Expose SDRConnect WebSocket frequency control as a rigctld-compatible TCP server."
    )
    parser.add_argument("--listen-host", default="0.0.0.0", help="rigctl TCP bind address")
    parser.add_argument("--listen-port", type=int, default=4532, help="rigctl TCP listen port")
    parser.add_argument("--sdr-port", type=int, default=5454, help="local SDRConnect WebSocket port")
    parser.add_argument("--device", default="primary", choices=["primary", "secondary"], help="SDRConnect device")
    parser.add_argument(
        "--frequency-property",
        default="device_vfo_frequency",
        choices=["device_vfo_frequency", "device_center_frequency"],
        help="SDRConnect frequency property to control",
    )
    parser.add_argument(
        "--refresh-interval",
        type=float,
        default=0.25,
        help="seconds between background SDRConnect frequency cache refreshes; 0 disables it",
    )
    parser.add_argument("--request-timeout", type=float, default=1.5, help="seconds to wait for get_property")
    parser.add_argument("--verbose", action="store_true", help="enable debug logging")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        force=True,
    )
    state = BridgeState()
    sdr = SDRConnectClient(
        port=args.sdr_port,
        device=args.device,
        frequency_property=args.frequency_property,
        state=state,
        request_timeout=args.request_timeout,
        refresh_interval=args.refresh_interval,
    )
    rigctl = RigctlServer(args.listen_host, args.listen_port, state, sdr)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signame in ("SIGINT", "SIGTERM"):
        if hasattr(signal, signame):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(getattr(signal, signame), stop.set)

    sdr_task = asyncio.create_task(sdr.run())
    rigctl_task = asyncio.create_task(rigctl.run())
    await stop.wait()
    sdr.stop()
    for task in (sdr_task, rigctl_task):
        task.cancel()
    await asyncio.gather(sdr_task, rigctl_task, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
