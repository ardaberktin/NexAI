"""Expose an isolated llama-server's loopback on a private Unix socket."""

import os
import select
import socket
import socketserver
import subprocess
import sys
import threading


class Relay(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            with socket.create_connection(("127.0.0.1", 7787), timeout=170) as backend:
                sockets = {backend: self.request, self.request: backend}
                while readable := select.select(list(sockets), [], [], 175)[0]:
                    for source in readable:
                        chunk = source.recv(65536)
                        if not chunk:
                            return
                        sockets[source].sendall(chunk)
        except OSError:
            return


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


if __name__ == "__main__":
    path = "/nex/work/model.sock"
    os.umask(0o077)
    child = subprocess.Popen(sys.argv[1:], stdin=subprocess.DEVNULL)
    try:
        with Server(path, Relay) as server:
            threading.Thread(target=lambda: (child.wait(), server.shutdown()), daemon=True).start()
            server.serve_forever()
    finally:
        if child.poll() is None:
            child.terminate()
