"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

from discord import DiscordException


__all__ = ('IPCError', 'NoEndpointFoundError', 'JSONEncodeError', 'NotConnected')


class IPCError(DiscordException):
    pass


class NoEndpointFoundError(IPCError):
    pass


class ServerConnectionRefusedError(IPCError):
    pass


class JSONEncodeError(IPCError):
    pass


class NotConnected(IPCError):
    pass
