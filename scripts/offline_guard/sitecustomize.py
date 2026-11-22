"""Inherited by Python subprocesses of registered offline test suites only."""
import os
import socket
if os.environ.get("BINGGO_OFFLINE") == "1":
    _connect, _connect_ex = socket.socket.connect, socket.socket.connect_ex
    def _check(address):
        if isinstance(address, tuple) and str(address[0]) not in {"127.0.0.1", "::1", "localhost"}:
            raise AssertionError("Offline worker blocked an external socket")
    def connect(self, address):
        _check(address)
        return _connect(self, address)
    def connect_ex(self, address):
        _check(address)
        return _connect_ex(self, address)
    socket.socket.connect, socket.socket.connect_ex = connect, connect_ex
    if os.name == "nt":
        _mkdir = os.mkdir
        def mkdir(path, mode=0o777, *, dir_fd=None):
            return _mkdir(path, 0o777 if mode == 0o700 else mode, dir_fd=dir_fd)
        os.mkdir = mkdir
