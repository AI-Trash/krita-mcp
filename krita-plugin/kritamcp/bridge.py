"""Qt-independent, bounded handoff from HTTP workers to the GUI thread."""
from __future__ import annotations

from collections import deque
from enum import Enum
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import socket
from socketserver import ThreadingMixIn
from threading import BoundedSemaphore, Condition, Event, Lock
from time import monotonic
from typing import Callable, Final, Protocol, TypeAlias
from urllib.parse import urlsplit

Json: TypeAlias = str | int | float | bool | None | list["Json"] | dict[str, "Json"]
Result: TypeAlias = dict[str, Json]
VERSION: Final = "0.3.1"
MAX_PENDING: Final = 32


class BatchWork(Protocol):
    def step(self, checkpoint: Callable[[], None]) -> Result | None: ...
    def stop(self) -> Result: ...


class CommandError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)

    def __str__(self) -> str:
        return self.message


class State(Enum):
    QUEUED = 1
    RUNNING = 2
    COMPLETE = 3
    CANCELLED = 4


class Request:
    """Mutable synchronization state belonging to exactly one caller."""

    def __init__(self, command, timeout: float):
        self.command = command
        self.deadline = None if getattr(command, "is_batch", False) else monotonic() + timeout
        self.condition = Condition()
        self.state = State.QUEUED
        self.result: Result = {}
        self.stopping = Event()
        self.batch: BatchWork | None = None

    def start(self) -> bool:
        with self.condition:
            if self.state is not State.QUEUED:
                return False
            if self.deadline is not None and monotonic() >= self.deadline:
                self.cancel()
                return False
            self.state = State.RUNNING
            return True

    def cancel(self) -> None:
        with self.condition:
            self.stopping.set()
            if self.state is State.QUEUED:
                self.state = State.CANCELLED
                self.result = {"error": "Queued command cancelled before execution", "execution_state": "cancelled",
                               "outcome_uncertain": False, "retry_safe": True}
                self.condition.notify_all()

    def checkpoint(self) -> None:
        if self.stopping.is_set():
            raise CommandError("Bridge is shutting down; execution stopped before next mutation")
        if self.deadline is not None and monotonic() >= self.deadline:
            raise CommandError("Request deadline exceeded; execution stopped at checkpoint. Earlier mutations may remain; do not retry automatically")

    def finish(self, result: Result) -> None:
        with self.condition:
            self.result = result
            self.state = State.COMPLETE
            self.condition.notify_all()

    def wait(self) -> Result:
        with self.condition:
            self.condition.wait_for(
                lambda: self.state in (State.COMPLETE, State.CANCELLED),
                None if self.deadline is None else max(0, self.deadline - monotonic()),
            )
            if self.state is State.QUEUED:
                self.cancel()
            if self.state is State.RUNNING:
                return {"error": "Command already executing; outcome unknown. Do not retry automatically",
                        "execution_state": "running", "outcome_uncertain": True, "retry_safe": False}
            return self.result


class CommandQueue:
    """Bounded FIFO; only the GUI timer calls process_one."""

    def __init__(self, capacity: int = MAX_PENDING):
        self.capacity = capacity
        self.queue: deque[Request] = deque()
        self.lock = Lock()
        self.closed = False
        self.processing = False
        self.active: Request | None = None

    def submit(self, command, timeout: float = 120) -> Request:
        request = Request(command, timeout)
        with self.lock:
            self.queue = deque(item for item in self.queue if item.state is State.QUEUED)
            if self.closed:
                raise CommandError("Bridge is shutting down")
            if len(self.queue) >= self.capacity:
                raise CommandError("Command queue full; retry after pending work finishes")
            self.queue.append(request)
        return request

    def process_one(self, execute: Callable[..., Result | BatchWork]) -> bool:
        with self.lock:
            if self.processing or (self.active is None and not self.queue):
                return False
            request = self.active if self.active is not None else self.queue.popleft()
            self.active = request
            self.processing = True
        try:
            if request.state is State.RUNNING or request.start():
                try:
                    if request.batch is None:
                        started = execute(request.command, request.checkpoint)
                        if isinstance(started, dict):
                            request.finish(started)
                            return True
                        request.batch = started
                    result = request.batch.step(request.checkpoint)
                except Exception as error:
                    import traceback
                    traceback.print_exc()
                    result = {"error": str(error), "outcome_uncertain": True, "retry_safe": False}
                if result is not None:
                    request.finish(result)
        finally:
            with self.lock:
                if self.closed and request.state is State.RUNNING and request.batch is not None:
                    request.finish(request.batch.stop())
                if request.state in (State.COMPLETE, State.CANCELLED):
                    self.active = None
                    request.batch = None
                self.processing = False
        return True

    def close(self) -> None:
        with self.lock:
            self.closed = True
            for request in self.queue:
                request.cancel()
            self.queue.clear()
            if self.active is not None:
                self.active.cancel()
                if not self.processing and self.active.batch is not None:
                    self.active.finish(self.active.batch.stop())
                    self.active.batch = None
                    self.active = None


class BridgeServer(ThreadingMixIn, HTTPServer):
    """Loopback HTTP with a hard connection cap, not a request-rate throttle."""

    daemon_threads = True
    request_queue_size = MAX_PENDING
    allow_reuse_address = True

    def __init__(self, port: int, queue: CommandQueue, prepare: Callable, info: Result):
        self.queue = queue
        self.prepare = prepare
        self.info = info
        self.slots = BoundedSemaphore(MAX_PENDING)
        self.serving = Event()
        super().__init__(("127.0.0.1", port), PaintRequestHandler)

    def service_actions(self):
        self.serving.set()

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(10)
        return connection, address

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                body = b'{"error":"HTTP capacity full"}'
                request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nContent-Type: application/json\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except (RuntimeError, OSError):
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class PaintRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def send_json_response(self, data: Result, status: int = 200):
        body = json.dumps(data, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def do_GET(self):
        match urlsplit(self.path).path:
            case "/health":
                self.send_json_response({"status": "ok", "plugin": "kritamcp", "version": VERSION})
            case "/info":
                self.send_json_response({"status": "ok", "version": VERSION, **self.server.info})
            case _:
                self.send_json_response({"error": "Unknown endpoint"}, 404)

    def do_POST(self):
        if "Origin" in self.headers:
            self.send_json_response({"error": "Origin headers are forbidden; use an originless native client"}, 403)
            return
        content_types = self.headers.get_all("Content-Type", [])
        if len(content_types) != 1 or content_types[0].partition(";")[0].strip().lower() != "application/json":
            self.send_json_response({"error": "Content-Type must be application/json"}, 415)
            return
        if urlsplit(self.path).path not in ("/", "/command"):
            self.send_json_response({"error": "Unknown endpoint"}, 404)
            return
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get("Transfer-Encoding") is not None or len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
            self.send_json_response({"error": "A single positive Content-Length is required"}, 400)
            return
        try:
            length = int(lengths[0])
        except ValueError:
            self.send_json_response({"error": "Invalid Content-Length"}, 400)
            return
        if length < 1:
            self.send_json_response({"error": "A positive Content-Length is required"}, 400)
            return
        try:
            body = self.rfile.read(length)
            if len(body) != length:
                raise CommandError("Incomplete payload")
            command = self.server.prepare(json.loads(body))
            del body
        except (CommandError, ValueError, TypeError, UnicodeError, RecursionError, OverflowError, socket.timeout) as error:
            self.send_json_response({"error": str(error)}, 400)
            return
        try:
            request = self.server.queue.submit(command)
        except CommandError as error:
            self.send_json_response({"error": str(error)}, 503)
            return
        result = request.wait()
        self.send_json_response(result, 500 if "error" in result else 200)
