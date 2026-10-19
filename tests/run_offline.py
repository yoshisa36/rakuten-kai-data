"""Run the full unit suite with socket networking blocked before discovery."""
import socket
import unittest
from pathlib import Path
from unittest.mock import patch


def deny_network(*args, **kwargs):
    raise AssertionError('Network access is forbidden in unit tests')


def main():
    root = Path(__file__).resolve().parents[1]
    with patch.object(socket.socket, 'connect', deny_network), \
         patch.object(socket.socket, 'connect_ex', deny_network), \
         patch.object(socket.socket, 'sendto', deny_network), \
         patch.object(socket, 'create_connection', deny_network), \
         patch.object(socket, 'getaddrinfo', deny_network):
        suite = unittest.defaultTestLoader.discover(str(root / 'tests'))
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1

if __name__ == '__main__':
    raise SystemExit(main())
