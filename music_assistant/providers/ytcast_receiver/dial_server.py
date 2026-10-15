"""
DIAL (Discovery And Launch) protocol server: SSDP discovery + HTTP app-launch endpoints.

Ported from yt-cast-receiver's DialServer.ts, which itself delegates the actual protocol work to
the `@patrickkfkan/peer-dial` npm package. This module re-implements peer-dial's wire behavior
directly against the public DIAL spec (https://www.dial-multiscreen-org/) and peer-dial's own
source (read directly, not just its README) for the exact SSDP headers, XML shape, and HTTP
status codes YouTube's sender apps expect - none of that is documented in the DIAL spec at the
byte level, so fidelity to peer-dial's actual behavior matters more than "spec-compliant enough".
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import uuid as uuid_mod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol
from xml.sax.saxutils import escape

from aiohttp import web

if TYPE_CHECKING:
    from logging import Logger

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900

_SERVICE_TYPES = (
    "urn:dial-multiscreen-org:service:dial:1",
    "urn:dial-multiscreen-org:device:dial:1",
    "upnp:rootdevice",
    "ssdp:all",
)

_SERVER_HEADER = "MusicAssistant UPnP/1.1 ytcast-receiver/1.0"
_ALIVE_REPEAT = (
    3  # send each alive notification this many times on (re)announce, per common SSDP practice
)
_ALIVE_REANNOUNCE_INTERVAL = 15 * 60  # seconds


class DialApp(Protocol):
    """Interface a delegate app must satisfy to be launchable via DIAL (mirrors dial.App in TS)."""

    name: str
    state: str  # "stopped" | "starting" | "running"
    allow_stop: bool
    pid: str | None

    async def launch(self, launch_data: str) -> str:
        """Handle a DIAL launch request; return the pid to report back to the sender."""
        ...


@dataclass
class DialServerOptions:
    """Configuration for a DialServer instance."""

    host: str
    port: int
    friendly_name: str
    manufacturer: str
    model_name: str
    prefix: str = "/ytcr"
    device_uuid: str | None = None


class DialServer:
    """Advertises a DIAL-discoverable receiver device and dispatches app launch requests."""

    def __init__(
        self, apps: dict[str, DialApp], options: DialServerOptions, logger: Logger
    ) -> None:
        """
        Initialize the server (does not bind/start anything yet - call `start()`).

        :param apps: Map of DIAL app name (e.g. "YouTube") to the DialApp handling it.
        :param options: Network/identity configuration.
        :param logger: Logger to use.
        """
        self._apps = apps
        self._opts = options
        self._logger = logger
        self._uuid = options.device_uuid or str(uuid_mod.uuid4())
        self._runner: web.AppRunner | None = None
        self._ssdp_transport: asyncio.DatagramTransport | None = None
        self._reannounce_task: asyncio.Task[None] | None = None
        self._status = "stopped"

    @property
    def location_url(self) -> str:
        """The URL advertised in SSDP LOCATION / used as the DIAL device-description URL."""
        return f"http://{self._opts.host}:{self._opts.port}{self._opts.prefix}/ssdp/device-desc.xml"

    async def start(self) -> None:
        """Start the HTTP server and SSDP responder, and announce presence."""
        if self._status != "stopped":
            return
        self._status = "starting"

        app = web.Application()
        p = self._opts.prefix
        app.router.add_get(f"{p}/ssdp/device-desc.xml", self._handle_device_desc)
        app.router.add_get(f"{p}/ssdp/notfound", self._handle_not_found)
        app.router.add_get(f"{p}/apps", self._handle_apps_root)
        app.router.add_get(f"{p}/apps/{{app_name}}", self._handle_get_app)
        app.router.add_post(f"{p}/apps/{{app_name}}", self._handle_post_app)
        app.router.add_post(f"{p}/apps/{{app_name}}/dial_data", self._handle_dial_data)
        app.router.add_delete(f"{p}/apps/{{app_name}}/{{pid}}", self._handle_delete_app)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        # only the advertised address: SSDP's LOCATION header points the phone there
        site = web.TCPSite(self._runner, self._opts.host, self._opts.port)
        await site.start()

        await self._start_ssdp()
        self._announce_alive()
        self._reannounce_task = asyncio.create_task(self._reannounce_loop())

        self._status = "running"
        self._logger.info("DIAL server listening on port %s", self._opts.port)

    async def stop(self) -> None:
        """Announce departure and tear down the HTTP/SSDP listeners."""
        if self._status != "running":
            return
        self._status = "stopping"

        if self._reannounce_task:
            self._reannounce_task.cancel()
            self._reannounce_task = None

        self._announce_byebye()

        if self._ssdp_transport:
            self._ssdp_transport.close()
            self._ssdp_transport = None
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

        self._status = "stopped"

    # -- SSDP ------------------------------------------------------------------

    async def _start_ssdp(self) -> None:
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            with contextlib.suppress(OSError):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.bind(("0.0.0.0", SSDP_PORT))  # noqa: S104 - receiving multicast needs the wildcard
        mreq = socket.inet_aton(SSDP_ADDR) + socket.inet_aton(self._opts.host)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
        sock.setblocking(False)

        server = self

        class _Protocol(asyncio.DatagramProtocol):
            def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
                server._on_datagram(data, addr)

        transport, _ = await loop.create_datagram_endpoint(lambda: _Protocol(), sock=sock)
        self._ssdp_transport = transport

    def _on_datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            text = data.decode("utf-8", errors="replace")
        except UnicodeDecodeError:
            return
        lines = text.split("\r\n")
        if not lines or not lines[0].upper().startswith("M-SEARCH"):
            return
        headers = _parse_headers(lines[1:])
        man = headers.get("man", "").strip('"')
        st = headers.get("st", "")
        if man != "ssdp:discover":
            return
        if st in _SERVICE_TYPES or st == f"uuid:{self._uuid}":
            self._logger.debug("SSDP M-SEARCH for %s from %s", st, addr)
            self._reply(st, addr)

    def _reply(self, st: str, addr: tuple[str, int]) -> None:
        headers = (
            "HTTP/1.1 200 OK\r\n"
            f"LOCATION: {self.location_url}\r\n"
            f"ST: {st}\r\n"
            "CACHE-CONTROL: max-age=1800\r\n"
            "CONFIGID.UPNP.ORG: 7337\r\n"
            "BOOTID.UPNP.ORG: 7337\r\n"
            f"SERVER: {_SERVER_HEADER}\r\n"
            f"USN: uuid:{self._uuid}::{st}\r\n"
            "\r\n"
        )
        if self._ssdp_transport:
            self._ssdp_transport.sendto(headers.encode("utf-8"), addr)

    def _announce_alive(self) -> None:
        for _ in range(_ALIVE_REPEAT):
            for st in (*_SERVICE_TYPES, f"uuid:{self._uuid}"):
                self._notify("ssdp:alive", st)

    def _announce_byebye(self) -> None:
        for st in (*_SERVICE_TYPES, f"uuid:{self._uuid}"):
            self._notify("ssdp:byebye", st)

    def _notify(self, nts: str, st: str) -> None:
        msg = (
            "NOTIFY * HTTP/1.1\r\n"
            f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
            "CACHE-CONTROL: max-age=1800\r\n"
            f"LOCATION: {self.location_url}\r\n"
            f"SERVER: {_SERVER_HEADER}\r\n"
            f"NT: {st}\r\n"
            f"NTS: {nts}\r\n"
            f"USN: uuid:{self._uuid}::{st}\r\n"
            "\r\n"
        )
        if self._ssdp_transport:
            self._ssdp_transport.sendto(msg.encode("utf-8"), (SSDP_ADDR, SSDP_PORT))

    async def _reannounce_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(_ALIVE_REANNOUNCE_INTERVAL)
                self._announce_alive()
        except asyncio.CancelledError:
            pass

    # -- HTTP --------------------------------------------------------------

    async def _handle_device_desc(self, request: web.Request) -> web.Response:
        del request
        xml = _DEVICE_DESC_XML.format(
            url_base=escape(f"http://{self._opts.host}:{self._opts.port}{self._opts.prefix}"),
            friendly_name=escape(self._opts.friendly_name),
            manufacturer=escape(self._opts.manufacturer),
            model_name=escape(self._opts.model_name),
            uuid=self._uuid,
        )
        return web.Response(
            text=xml,
            content_type="application/xml",
            headers={
                "Application-URL": f"http://{self._opts.host}:{self._opts.port}{self._opts.prefix}/apps"
            },
        )

    async def _handle_not_found(self, request: web.Request) -> web.Response:
        del request
        return web.Response(status=404)

    async def _handle_apps_root(self, request: web.Request) -> web.Response:
        del request
        return web.Response(status=204)

    async def _handle_get_app(self, request: web.Request) -> web.Response:
        app_name = request.match_info["app_name"]
        app = self._apps.get(app_name)
        if not app:
            return web.Response(status=404)
        state = app.state or ("running" if app.pid else "stopped")
        xml = _APP_DESC_XML.format(
            name=escape(app_name),
            allow_stop=str(app.allow_stop).lower(),
            state=state,
            href=escape(app.pid) if app.pid else "",
        )
        return web.Response(text=xml, content_type="application/xml")

    async def _handle_post_app(self, request: web.Request) -> web.Response:
        app_name = request.match_info["app_name"]
        app = self._apps.get(app_name)
        if not app:
            return web.Response(status=404)

        body = await request.text()
        was_stopped = (app.state or ("running" if app.pid else "stopped")) == "stopped"
        try:
            pid = await app.launch(body)
        except Exception as err:
            self._logger.error("DIAL launch failed for app %s: %s", app_name, err)
            return web.Response(status=503)

        status = 201 if was_stopped else 200
        headers = {}
        if pid:
            base = f"http://{self._opts.host}:{self._opts.port}{self._opts.prefix}"
            headers["LOCATION"] = f"{base}/apps/{app_name}/{pid}"
        return web.Response(status=status, headers=headers)

    async def _handle_dial_data(self, request: web.Request) -> web.Response:
        del request
        return web.Response(status=501)

    async def _handle_delete_app(self, request: web.Request) -> web.Response:
        app_name = request.match_info["app_name"]
        app = self._apps.get(app_name)
        if not app:
            return web.Response(status=404)
        if not app.allow_stop:
            return web.Response(status=405)
        return web.Response(status=400)


def _parse_headers(lines: list[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in lines:
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        headers[key.strip().lower()] = value.strip()
    return headers


_DEVICE_DESC_XML = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <specVersion>
    <major>1</major>
    <minor>0</minor>
  </specVersion>
  <URLBase>{url_base}</URLBase>
  <device>
    <deviceType>urn:dial-multiscreen-org:device:dial:1</deviceType>
    <friendlyName>{friendly_name}</friendlyName>
    <manufacturer>{manufacturer}</manufacturer>
    <modelName>{model_name}</modelName>
    <UDN>uuid:{uuid}</UDN>
    <serviceList>
      <service>
        <serviceType>urn:dial-multiscreen-org:service:dial:1</serviceType>
        <serviceId>urn:dial-multiscreen-org:serviceId:dial</serviceId>
        <controlURL>/ssdp/notfound</controlURL>
        <eventSubURL>/ssdp/notfound</eventSubURL>
        <SCPDURL>/ssdp/notfound</SCPDURL>
      </service>
    </serviceList>
  </device>
</root>
"""

_APP_DESC_XML = """<?xml version="1.0" encoding="UTF-8"?>
<service xmlns="urn:dial-multiscreen-org:schemas:dial" dialVer="1.7">
  <name>{name}</name>
  <options allowStop="{allow_stop}"/>
  <state>{state}</state>
  <link rel="run" href="{href}" />
</service>
"""
