"""A stdio client that reads what the server actually wrote.

An SDK client hides transport noise: it skips lines it cannot parse, fills in
fields, and keeps going. That is the right behaviour for a client and the wrong
behaviour for a test, so this one records every deviation instead — the stray
`print()` that corrupts the stream is a finding, not an inconvenience.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .transcript import Exchange, Transcript


class WireTimeout(RuntimeError):
    """The server did not answer within the deadline."""


@dataclass
class StdioWire:
    command: list[str]
    env: dict[str, str] | None = None
    cwd: str | None = None
    read_timeout_s: float = 15.0

    transcript: Transcript = field(default_factory=Transcript)
    _process: subprocess.Popen[bytes] | None = field(default=None, init=False, repr=False)
    _inbox: queue.Queue[dict[str, Any]] = field(default_factory=queue.Queue, init=False, repr=False)
    _pending: dict[Any, dict[str, Any]] = field(default_factory=dict, init=False, repr=False)
    _stderr: list[str] = field(default_factory=list, init=False, repr=False)

    # ------------------------------------------------------------- lifecycle
    def start(self) -> StdioWire:
        environment = {**os.environ, **(self.env or {})}
        self._process = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            cwd=self.cwd,
        )
        threading.Thread(target=self._read_stdout, daemon=True, name="mcpqa-stdout").start()
        threading.Thread(target=self._read_stderr, daemon=True, name="mcpqa-stderr").start()
        return self

    def close(self, timeout: float = 5.0) -> int | None:
        """Close stdin and let the server exit, as the spec says a client should."""
        process = self._process
        if process is None:
            return None
        if process.stdin is not None and not process.stdin.closed:
            with contextlib.suppress(BrokenPipeError):  # the server may already be gone
                process.stdin.close()
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            code = process.wait(timeout=timeout)
            self.transcript.stderr = self.stderr_text()
            return code
        self.transcript.stderr = self.stderr_text()
        return code

    def kill(self) -> None:
        if self._process is not None and self._process.poll() is None:
            self._process.kill()
            self._process.wait(timeout=5)

    @property
    def returncode(self) -> int | None:
        return None if self._process is None else self._process.poll()

    def stderr_text(self) -> str:
        return "".join(self._stderr)

    # ---------------------------------------------------------------- pumps
    def _read_stdout(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        for raw in process.stdout:
            line = raw.decode("utf-8", errors="replace")
            stripped = line.strip()
            if not stripped:
                continue
            try:
                message = json.loads(stripped)
            except json.JSONDecodeError:
                # The spec is unambiguous: a stdio server MUST NOT write
                # anything to stdout that is not a valid MCP message.
                self.transcript.stdout_violations.append(stripped)
                continue
            if not isinstance(message, dict):
                self.transcript.stdout_violations.append(stripped)
                continue
            self._inbox.put(message)

    def _read_stderr(self) -> None:
        process = self._process
        assert process is not None and process.stderr is not None
        for raw in process.stderr:
            self._stderr.append(raw.decode("utf-8", errors="replace"))

    # -------------------------------------------------------------- sending
    def send(self, message: dict[str, Any]) -> str:
        process = self._process
        if process is None or process.stdin is None:
            raise RuntimeError("wire is not started")
        raw = json.dumps(message, ensure_ascii=False)
        process.stdin.write((raw + "\n").encode("utf-8"))
        process.stdin.flush()
        return raw

    def send_raw(self, payload: str) -> None:
        """Write bytes verbatim — for malformed-input checks."""
        process = self._process
        if process is None or process.stdin is None:
            raise RuntimeError("wire is not started")
        process.stdin.write(payload.encode("utf-8"))
        process.stdin.flush()

    def notify(self, message: dict[str, Any]) -> Exchange:
        raw = self.send(message)
        return self.transcript.add(Exchange(request=message, response=None, raw_request=raw))

    def request(self, message: dict[str, Any], *, timeout: float | None = None) -> Exchange:
        started = time.monotonic()
        raw = self.send(message)
        response = self.await_response(message["id"], timeout=timeout)
        return self.transcript.add(
            Exchange(
                request=message,
                response=response,
                raw_request=raw,
                raw_response=json.dumps(response, ensure_ascii=False),
                elapsed_s=time.monotonic() - started,
            )
        )

    # ------------------------------------------------------------ receiving
    def await_response(self, request_id: Any, *, timeout: float | None = None) -> dict[str, Any]:
        """Wait for the response with this id, parking anything else."""
        deadline = time.monotonic() + (timeout if timeout is not None else self.read_timeout_s)
        if request_id in self._pending:
            return self._pending.pop(request_id)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WireTimeout(f"no response for request id {request_id!r}")
            try:
                message = self._inbox.get(timeout=min(0.25, remaining))
            except queue.Empty:
                if self.returncode is not None and self._inbox.empty():
                    raise WireTimeout(
                        f"server exited with code {self.returncode} before answering {request_id!r}"
                    ) from None
                continue
            if "method" in message:
                # A request or notification from the server. Requests carry ids
                # of their own, which may collide with ours; they are never the
                # answer to anything we sent.
                self.transcript.unsolicited.append(message)
                continue
            if "id" in message and message.get("id") == request_id:
                return message
            if "id" in message and message["id"] is not None:
                self._pending[message["id"]] = message
            else:
                self.transcript.unsolicited.append(message)

    def drain(self, window_s: float = 0.4) -> list[dict[str, Any]]:
        """Collect whatever arrives in a short window (notifications, strays)."""
        collected: list[dict[str, Any]] = []
        deadline = time.monotonic() + window_s
        while time.monotonic() < deadline:
            try:
                collected.append(self._inbox.get(timeout=max(0.01, deadline - time.monotonic())))
            except queue.Empty:
                break
        return collected

    def __enter__(self) -> StdioWire:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()
