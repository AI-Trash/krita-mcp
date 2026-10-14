"""Register the HTTP bridge and drain its queue on Krita's GUI thread."""
import os

from krita import Extension, Krita
from PyQt5.QtCore import QThread, QTimer
from PyQt5.QtWidgets import QApplication

from .bridge import BridgeServer, CommandQueue
from .commands import Action, CommandExecutor, prepare

SERVER_PORT = int(os.environ.get("KRITA_MCP_PORT", "5678"))
if not 1 <= SERVER_PORT <= 65535:
    raise ValueError("KRITA_MCP_PORT must be in 1..65535")
CANVAS_OUTPUT_DIR = os.path.expanduser("~/krita-mcp-output")


class ServerThread(QThread):
    def __init__(self, server):
        super().__init__()
        self.server = server
        self.stopped = False

    def run(self):
        self.server.serve_forever()

    def stop(self):
        if self.stopped:
            return
        self.stopped = True
        if self.isRunning():
            self.server.serving.wait()
            self.server.shutdown()
        self.server.server_close()
        self.wait()


class KritaMCPExtension(Extension):
    def __init__(self, parent):
        super().__init__(parent)
        self.queue = CommandQueue()
        self.executor = CommandExecutor(Krita.instance(), CANVAS_OUTPUT_DIR)
        self.server_thread = None
        self.timer = None
        self.closed = False
        application = QApplication.instance()
        if application is not None:
            application.aboutToQuit.connect(self.cleanup)

    def setup(self):
        pass

    def createActions(self, window):
        if self.closed:
            return
        if self.server_thread is None:
            os.makedirs(CANVAS_OUTPUT_DIR, exist_ok=True)
            server = BridgeServer(SERVER_PORT, self.queue, prepare,
                                  {"canvas_dir": CANVAS_OUTPUT_DIR, "commands": [action.value for action in Action]})
            self.server_thread = ServerThread(server)
            self.server_thread.start()
            self.timer = QTimer(self)
            self.timer.timeout.connect(self.process_commands)
            self.timer.start(25)

    def process_commands(self):
        self.queue.process_one(self.executor.begin)

    def cleanup(self):
        if self.closed:
            return
        self.closed = True
        if self.timer is not None:
            self.timer.stop()
        self.queue.close()
        if self.server_thread is not None:
            self.server_thread.stop()


Krita.instance().addExtension(KritaMCPExtension(Krita.instance()))
