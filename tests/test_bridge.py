from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import http.client
import importlib.util
import json
from pathlib import Path
import sys
from threading import Event, Thread, get_ident
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "krita-plugin" / "kritamcp" / "bridge.py"
SPEC = importlib.util.spec_from_file_location("bridge_under_test", SOURCE)
bridge = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bridge
SPEC.loader.exec_module(bridge)


class QueueTests(unittest.TestCase):
    def test_expired_queued_request_never_executes(self):
        queue = bridge.CommandQueue()
        request = queue.submit({"action": "undo"}, timeout=0)
        called = []

        queue.process_one(lambda *args: called.append(args))

        self.assertEqual(called, [])
        self.assertEqual(request.state, bridge.State.CANCELLED)
        self.assertIn("error", request.wait())

    def test_wait_cancels_queued_work_before_mutation(self):
        request = bridge.Request({}, timeout=0)

        result = request.wait()

        self.assertIn("error", result)
        self.assertFalse(request.start())

    def test_running_deadline_reports_unknown_outcome(self):
        with patch.object(bridge, "monotonic", return_value=0) as clock:
            request = bridge.Request({}, timeout=10)
            self.assertTrue(request.start())
            clock.return_value = 11

            result = request.wait()

        self.assertEqual(result["execution_state"], "running")
        self.assertIn("error", result)

    def test_checkpoint_stops_between_mutations(self):
        with patch.object(bridge, "monotonic", return_value=0) as clock:
            request = bridge.Request({}, timeout=10)
            request.start()
            request.checkpoint()
            clock.return_value = 11

            with self.assertRaises(bridge.CommandError):
                request.checkpoint()

    def test_batch_checkpoint_and_wait_survive_elapsed_deadline(self):
        with patch.object(bridge, "monotonic", return_value=0) as clock:
            request = bridge.Request(SimpleNamespace(is_batch=True), timeout=120)
            request.start()
            clock.return_value = 121
            request.checkpoint()
            with ThreadPoolExecutor(max_workers=1) as pool:
                waiting = Event()
                def wait():
                    waiting.set()
                    return request.wait()
                future = pool.submit(wait)
                self.assertTrue(waiting.wait(2))
                premature = future.done()
                request.finish({"completed_count": 2})
                result = future.result(2)

        self.assertFalse(premature)
        self.assertEqual(result, {"completed_count": 2})

    def test_queue_capacity_and_reclamation(self):
        queue = bridge.CommandQueue(capacity=1)
        first = queue.submit({})
        with self.assertRaises(bridge.CommandError):
            queue.submit({})
        first.cancel()

        second = queue.submit({})

        self.assertEqual(len(queue.queue), 1)
        self.assertIs(queue.queue[0], second)

    def test_each_request_has_its_own_result(self):
        queue = bridge.CommandQueue()
        first, second = queue.submit({"value": 1}), queue.submit({"value": 2})

        queue.process_one(lambda command, checkpoint: {"value": command["value"]})
        queue.process_one(lambda command, checkpoint: {"value": command["value"]})

        self.assertEqual(first.wait(), {"value": 1})
        self.assertEqual(second.wait(), {"value": 2})

    def test_close_cancels_pending_and_rejects_submission(self):
        queue = bridge.CommandQueue()
        request = queue.submit({})

        queue.close()

        self.assertEqual(request.state, bridge.State.CANCELLED)
        with self.assertRaises(bridge.CommandError):
            queue.submit({})

    def test_reentrant_gui_drain_cannot_interleave_commands(self):
        queue = bridge.CommandQueue()
        first, second = queue.submit({"value": 1}), queue.submit({"value": 2})
        nested = []
        def execute(command, checkpoint):
            nested.append(queue.process_one(lambda *args: {"status": "unexpected"}))
            return {"value": command["value"]}

        queue.process_one(execute)

        self.assertEqual(nested, [False])
        self.assertEqual(first.wait(), {"value": 1})
        self.assertEqual(second.state, bridge.State.QUEUED)
        queue.process_one(lambda command, checkpoint: {"value": command["value"]})
        self.assertEqual(second.wait(), {"value": 2})


class SignalledQueue(bridge.CommandQueue):
    def __init__(self):
        super().__init__()
        self.arrived = Event()

    def submit(self, *args, **kwargs):
        request = super().submit(*args, **kwargs)
        self.arrived.set()
        return request


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.queue = SignalledQueue()

        def prepare(raw):
            if not isinstance(raw, dict) or raw.get("action") != "undo" or not isinstance(raw.get("params", {}), dict):
                raise bridge.CommandError("Invalid action/params")
            return raw

        self.server = bridge.BridgeServer(0, self.queue, prepare, {"commands": ["undo"]})
        self.thread = Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.shutdown)

    def shutdown(self):
        self.queue.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=2)
        try:
            request_headers = ({"Content-Type": "application/json"} if method == "POST" else {}) | (headers or {})
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_health_and_info_are_real_http_and_loopback_only(self):
        status, health = self.request("GET", "/health")
        info_status, info = self.request("GET", "/info")

        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        self.assertEqual((status, info_status), (200, 200))
        self.assertEqual(health["status"], "ok")
        self.assertEqual(info["commands"], ["undo"])

    def test_post_runs_only_when_main_thread_drains_queue(self):
        caller_thread = get_ident()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.request, "POST", "/", b'{"action":"undo"}')
            self.assertTrue(self.queue.arrived.wait(2))
            self.assertFalse(future.done())

            self.queue.process_one(lambda command, checkpoint: {"status": "ok", "thread": get_ident()})
            status, result = future.result(2)

        self.assertEqual(status, 200)
        self.assertEqual(result["thread"], caller_thread)

    def test_any_origin_is_rejected_before_read_prepare_or_queue(self):
        with patch.object(self.server, "prepare") as prepare:
            for origin in ("https://hostile.example", "null", ""):
                with self.subTest(origin=origin):
                    status, result = self.request("POST", "/", b"", {"Origin": origin, "Content-Length": "100"})

                    self.assertEqual(status, 403)
                    self.assertIn("error", result)
            prepare.assert_not_called()
        self.assertFalse(self.queue.arrived.is_set())

    def test_text_plain_is_rejected_before_read_prepare_or_queue(self):
        with patch.object(self.server, "prepare") as prepare:
            status, result = self.request("POST", "/", b"", {"Content-Type": "text/plain", "Content-Length": "100"})

            self.assertEqual(status, 415)
            self.assertIn("error", result)
            prepare.assert_not_called()
        self.assertFalse(self.queue.arrived.is_set())

    def test_json_with_charset_is_accepted_without_origin(self):
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.request, "POST", "/", b'{"action":"undo"}', {"Content-Type": "application/json; charset=utf-8"})
            self.assertTrue(self.queue.arrived.wait(2))

            self.queue.process_one(lambda command, checkpoint: {"status": "ok"})
            status, result = future.result(2)

        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "ok")

    def test_invalid_payload_is_rejected_before_queueing(self):
        for body in (b"[]", b"null", b"not json", b'{"action":"exec"}', b'{"action":"undo","params":[]}'):
            with self.subTest(body=body):
                status, result = self.request("POST", "/", body)

                self.assertEqual(status, 400)
                self.assertIn("error", result)
        self.assertFalse(self.queue.arrived.is_set())

    def test_invalid_lengths_and_transfer_encoding(self):
        for length in ("-1", "abc", "0", "9" * 5000, str(2 ** 100)):
            with self.subTest(length=length):
                status, result = self.request("POST", "/", b"", {"Content-Length": length})

                self.assertIn(status, (400, 413))
                self.assertIn("error", result)
        status, _ = self.request("POST", "/", b"", {"Transfer-Encoding": "chunked"})
        self.assertEqual(status, 400)
        self.assertFalse(self.queue.arrived.is_set())

    def test_missing_duplicate_or_nonnative_headers_are_rejected(self):
        cases = (([("Content-Type", "application/json")], 400),
                 ([("Content-Type", "application/json"), ("Content-Length", "1"), ("Content-Length", "1")], 400),
                 ([("Content-Type", "application/json"), ("Content-Type", "text/plain"), ("Content-Length", "1")], 415),
                 ([("Content-Length", "1")], 415))
        for headers, expected in cases:
            with self.subTest(headers=headers):
                connection = http.client.HTTPConnection(*self.server.server_address, timeout=2)
                try:
                    connection.putrequest("POST", "/")
                    for name, value in headers:
                        connection.putheader(name, value)
                    connection.endheaders()
                    response = connection.getresponse()
                    result = json.loads(response.read())

                    self.assertEqual(response.status, expected)
                    self.assertIn("error", result)
                finally:
                    connection.close()
        self.assertFalse(self.queue.arrived.is_set())

    def test_valid_http_body_larger_than_two_mib_is_accepted(self):
        body = b'{"action":"undo"}' + b" " * (2 * 1024 * 1024)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.request, "POST", "/", body)
            arrived = self.queue.arrived.wait(1)
            if arrived:
                self.queue.process_one(lambda command, checkpoint: {"status": "ok"})
            status, result = future.result(2)

        self.assertTrue(arrived)
        self.assertEqual((status, result), (200, {"status": "ok"}))

    def test_closed_queue_returns_service_unavailable(self):
        self.queue.close()

        status, result = self.request("POST", "/", b'{"action":"undo"}')

        self.assertEqual(status, 503)
        self.assertIn("error", result)

    def test_more_than_sixty_requests_are_not_rate_limited(self):
        statuses = [self.request("GET", "/health")[0] for _ in range(65)]

        self.assertEqual(statuses, [200] * 65)

    def test_connection_cap_rejects_without_starting_worker(self):
        for _ in range(bridge.MAX_PENDING):
            self.server.slots.acquire()
        try:
            status, result = self.request("GET", "/health")

            self.assertEqual(status, 503)
            self.assertIn("error", result)
        finally:
            for _ in range(bridge.MAX_PENDING):
                self.server.slots.release()


if __name__ == "__main__":
    unittest.main()
