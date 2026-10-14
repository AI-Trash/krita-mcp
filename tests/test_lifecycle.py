from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


def probe(mode):
    import http.client
    import importlib.util
    import socket
    import sys
    from enum import Enum
    from types import ModuleType, SimpleNamespace
    from unittest.mock import patch

    from PyQt5.QtCore import QObject, QTimer
    from PyQt5.QtWidgets import QApplication

    application = QApplication([])
    fake_app = QObject()
    fake_app.extensions = []
    fake_app.addExtension = fake_app.extensions.append
    fake_krita = ModuleType("krita")
    fake_krita.Extension = QObject
    fake_krita.Krita = SimpleNamespace(instance=lambda: fake_app)
    sys.modules["krita"] = fake_krita
    package_name = "lifecycle_plugin_test"
    fake_commands = ModuleType(package_name + ".commands")
    fake_commands.Action = Enum("Action", {"UNDO": "undo"})
    fake_commands.CommandExecutor = lambda *args: SimpleNamespace(begin=lambda *args: {"status": "ok"})
    fake_commands.prepare = lambda command: command
    if mode in ("batch_yield", "batch_quit"):
        from test_commands import AppDouble, commands
        adapter = AppDouble()
        fake_commands.Action = commands.Action
        fake_commands.CommandExecutor = lambda app, output: commands.CommandExecutor(adapter, output)
        fake_commands.prepare = commands.prepare
    sys.modules[fake_commands.__name__] = fake_commands
    source = Path(__file__).resolve().parents[1] / "krita-plugin" / "kritamcp" / "__init__.py"
    spec = importlib.util.spec_from_file_location(package_name, source, submodule_search_locations=[str(source.parent)])
    plugin = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = plugin
    spec.loader.exec_module(plugin)
    plugin.SERVER_PORT = 0
    plugin.CANVAS_OUTPUT_DIR = os.environ["HOME"]
    extension = fake_app.extensions[0]
    if mode in ("batch_yield", "batch_quit"):
        colors = ("red", "green", "blue")
        request = extension.queue.submit(commands.prepare({"action": "batch", "params": {
            "commands": [{"action": "set_color", "params": {"color": color}} for color in colors]}}))
        pending = extension.queue.submit(commands.prepare({"action": "set_color", "params": {"color": "yellow"}}))
        extension.timer = QTimer(extension)
        extension.timer.timeout.connect(extension.process_commands)
        native_color = adapter.view.setForeGroundColor
        def set_color(color):
            native_color(color)
            QTimer.singleShot(0, extension.process_commands)
            application.processEvents()
        adapter.view.setForeGroundColor = set_color
        observations = []
        observed_turns = []
        def observe_turn():
            observed_turns.append(len(observations))
            if mode == "batch_quit":
                application.quit()
        def after_tick():
            observations.append(adapter.view.foreground.name())
            if len(observations) == 1:
                assert request.state.name == "RUNNING" and pending.state.name == "QUEUED"
                QTimer.singleShot(0, observe_turn)
            if request.state.name == "COMPLETE":
                application.quit()
        extension.timer.timeout.connect(after_tick)
        extension.timer.start(10)
        watchdog = QTimer()
        watchdog.setSingleShot(True)
        watchdog.timeout.connect(application.quit)
        watchdog.start(2000)
        application.exec_()
        watchdog.stop()
        extension.cleanup()
        result = request.wait()
        assert observed_turns == [1], observed_turns
        assert pending.state.name == "CANCELLED"
        assert not extension.timer.isActive()
        if mode == "batch_quit":
            assert observations == ["#ff0000"]
            assert result["completed_count"] == result["failing_index"] == 1
            assert not result["outcome_uncertain"]
        else:
            assert observations == ["#ff0000", "#008000", "#0000ff"], observations
            assert result["completed_count"] == 3
        print(f"{mode}: real timer yielded, prefix truthful, pending cancelled")
        return
    server = None
    if mode in ("running", "immediate"):
        extension.createActions(None)
        server = extension.server_thread.server
    elif mode == "never_started":
        server = plugin.BridgeServer(0, extension.queue, lambda command: command, {})
        extension.server_thread = plugin.ServerThread(server)
        extension.timer = QTimer(extension)
        extension.timer.start(25)
    request = extension.queue.submit({"action": "undo"})
    if server is not None:
        address = server.server_address
        assert address[0] == "127.0.0.1" and address[1] != 5678
    if mode == "running":
        connection = http.client.HTTPConnection(*address, timeout=2)
        try:
            connection.request("GET", "/health")
            response = connection.getresponse()
            assert response.status == 200
            response.read()
        finally:
            connection.close()
    if server is None:
        QTimer.singleShot(0, application.quit)
        application.exec_()
        extension.cleanup()
    else:
        thread = extension.server_thread
        with patch.object(server, "shutdown", wraps=server.shutdown) as shutdown, patch.object(server, "server_close", wraps=server.server_close) as close, patch.object(thread, "wait", wraps=thread.wait) as wait:
            QTimer.singleShot(0, application.quit)
            application.exec_()
            extension.cleanup()
            thread.stop()
            assert shutdown.call_count == (0 if mode == "never_started" else 1)
            assert close.call_count == 1
            assert wait.call_count == 1
        assert not thread.isRunning()
        if mode != "never_started":
            assert thread.isFinished()
        with socket.socket() as connection:
            connection.settimeout(1)
            assert connection.connect_ex(address) != 0
        assert not extension.timer.isActive()
    assert extension.closed and extension.queue.closed
    assert request.state.name == "CANCELLED"
    assert request.wait()["execution_state"] == "cancelled"
    print(f"{mode}: pending cancelled, listener closed, Qt cleanup complete")


class LifecycleTests(unittest.TestCase):
    def run_probe(self, mode):
        launcher = os.environ.get("KRITA_MCP_TEST_PYTHON", sys.executable)
        with tempfile.TemporaryDirectory(prefix="kritamcp-lifecycle-") as home:
            environment = os.environ | {
                "HOME": home, "CFFIXED_USER_HOME": home,
                "XDG_CONFIG_HOME": home, "XDG_DATA_HOME": home, "XDG_CACHE_HOME": home,
                "QT_QPA_PLATFORM": "offscreen", "KRITA_MCP_PORT": "5679",
            }
            for override, target in (("KRITA_MCP_TEST_PYTHONHOME", "PYTHONHOME"),
                                     ("KRITA_MCP_TEST_FRAMEWORK_PATH", "DYLD_FRAMEWORK_PATH")):
                if override in os.environ:
                    environment[target] = os.environ[override]
            try:
                runtime = subprocess.run([launcher, "-B", "-c", "from PyQt5.QtWidgets import QApplication"],
                                         env=environment, capture_output=True, text=True, timeout=10)
            except (OSError, subprocess.TimeoutExpired) as error:
                raise unittest.SkipTest(f"Qt Python runtime unavailable: {error}; set KRITA_MCP_TEST_PYTHON") from None
            if runtime.returncode != 0:
                raise unittest.SkipTest("Qt Python runtime cannot import PyQt5; set KRITA_MCP_TEST_PYTHON and optional KRITA_MCP_TEST_PYTHONHOME/KRITA_MCP_TEST_FRAMEWORK_PATH")
            code = f"import sys; sys.path.insert(0, {str(Path(__file__).parent)!r}); from test_lifecycle import probe; probe({mode!r})"

            result = subprocess.run([launcher, "-B", "-c", code], env=environment, capture_output=True, text=True, timeout=10)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_quit_cancels_pending_closes_listener_and_waits_for_qthread(self):
        self.run_probe("running")

    def test_immediate_quit_handles_server_startup_race(self):
        self.run_probe("immediate")

    def test_cleanup_of_never_started_thread_does_not_shutdown_or_block(self):
        self.run_probe("never_started")

    def test_quit_before_actions_is_idempotent(self):
        self.run_probe("unused")

    def test_real_timer_yields_between_batch_children_without_interleaving(self):
        self.run_probe("batch_yield")

    def test_qt_quit_finishes_active_batch_prefix_without_later_mutation(self):
        self.run_probe("batch_quit")


if __name__ == "__main__":
    unittest.main()
