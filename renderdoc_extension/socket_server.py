"""
File-based IPC Server for RenderDoc MCP Bridge
Uses file polling since RenderDoc's Python doesn't have socket/QtNetwork modules.
"""

import csv
import json
import os
import traceback
import tempfile

from PySide2.QtCore import QObject, QTimer


# IPC directory
IPC_DIR = os.path.join(tempfile.gettempdir(), "renderdoc_mcp")
REQUEST_FILE = os.path.join(IPC_DIR, "request.json")
RESPONSE_FILE = os.path.join(IPC_DIR, "response.json")
RESPONSE_TMP_FILE = os.path.join(IPC_DIR, "response.json.tmp")
LOCK_FILE = os.path.join(IPC_DIR, "lock")
RESPONSE_LOCK_FILE = os.path.join(IPC_DIR, "response.lock")


class MCPBridgeServer(QObject):
    """File-based IPC server for MCP bridge communication"""

    def __init__(self, host, port, handler, parent=None):
        super(MCPBridgeServer, self).__init__(parent)
        self.handler = handler
        self._timer = None
        self._running = False

        # Create IPC directory
        if not os.path.exists(IPC_DIR):
            os.makedirs(IPC_DIR)

    def start(self):
        """Start the server with polling"""
        self._running = True

        # Clean up old files
        self._cleanup_files()

        # Start polling timer (check every 100ms)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll_request)
        self._timer.start(100)

        print("[MCP Bridge] File-based IPC server started")
        print("[MCP Bridge] IPC directory: %s" % IPC_DIR)
        return True

    def stop(self):
        """Stop the server"""
        self._running = False
        if self._timer:
            self._timer.stop()
            self._timer = None
        self._cleanup_files()
        print("[MCP Bridge] Server stopped")

    def is_running(self):
        """Check if server is running"""
        return self._running

    def _cleanup_files(self):
        """Remove IPC files"""
        for f in [
            REQUEST_FILE,
            RESPONSE_FILE,
            RESPONSE_TMP_FILE,
            LOCK_FILE,
            RESPONSE_LOCK_FILE,
        ]:
            try:
                if os.path.exists(f):
                    os.remove(f)
            except Exception:
                pass

    def _write_response(self, response):
        """Atomically write response JSON to avoid partial reads."""
        with open(RESPONSE_LOCK_FILE, "w") as lock_file:
            lock_file.write("lock")

        try:
            with open(RESPONSE_TMP_FILE, "w", encoding="utf-8") as f:
                json.dump(response, f, ensure_ascii=False)

            if os.path.exists(RESPONSE_FILE):
                os.remove(RESPONSE_FILE)
            os.rename(RESPONSE_TMP_FILE, RESPONSE_FILE)
        finally:
            try:
                if os.path.exists(RESPONSE_LOCK_FILE):
                    os.remove(RESPONSE_LOCK_FILE)
            except Exception:
                pass
            try:
                if os.path.exists(RESPONSE_TMP_FILE):
                    os.remove(RESPONSE_TMP_FILE)
            except Exception:
                pass

    def _poll_request(self):
        """Check for incoming request"""
        if not self._running:
            return

        # Check if request file exists
        if not os.path.exists(REQUEST_FILE):
            return

        # Check if lock file exists (client is still writing)
        if os.path.exists(LOCK_FILE):
            return

        try:
            # Read request
            with open(REQUEST_FILE, "r", encoding="utf-8") as f:
                request = json.load(f)

            # Remove request file
            os.remove(REQUEST_FILE)

            # Process request
            try:
                response = self.handler.handle(request)
            except Exception as e:
                traceback.print_exc()
                response = {
                    "id": request.get("id"),
                    "error": {"code": -32603, "message": str(e)}
                }

            # Write response
            self._write_response(response)

        except Exception as e:
            print("[MCP Bridge] Error processing request: %s" % str(e))
            traceback.print_exc()
