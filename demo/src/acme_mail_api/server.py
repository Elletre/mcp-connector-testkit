"""Run the provider: in a background thread for tests, or standalone."""

from __future__ import annotations

import threading
import time

import uvicorn

from .app import ProviderState, create_app


class ProviderServer:
    """The provider on an ephemeral port, started and stopped from tests.

    Binding to port 0 and asking the socket which port it got avoids the
    "find a free port, then race someone else to it" flake.
    """

    def __init__(self, *, control_enabled: bool = True, state: ProviderState | None = None) -> None:
        self.state = state or ProviderState()
        self.app = create_app(control_enabled=control_enabled, state=self.state)
        self._config = uvicorn.Config(self.app, host="127.0.0.1", port=0, log_level="warning")
        self._server = uvicorn.Server(self._config)
        self._thread: threading.Thread | None = None
        self._port: int | None = None

    @property
    def port(self) -> int:
        if self._port is None:
            raise RuntimeError("server is not started")
        return self._port

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self, timeout: float = 10.0) -> ProviderServer:
        self._thread = threading.Thread(target=self._server.run, name="acme-mail-api", daemon=True)
        self._thread.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._server.started and self._server.servers:
                self._port = self._server.servers[0].sockets[0].getsockname()[1]
                return self
            time.sleep(0.01)
        raise RuntimeError("provider did not start in time")

    def stop(self, timeout: float = 10.0) -> None:
        self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def __enter__(self) -> ProviderServer:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()
