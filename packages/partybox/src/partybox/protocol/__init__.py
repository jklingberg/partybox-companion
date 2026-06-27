"""Vendor protocol codec and typed message types.

Usage::

    from partybox.protocol import encode, PowerCommand, PowerState

    frame = encode(PowerCommand(PowerState.ON))   # b'\\xaa\\x03\\x01\\x05'
"""

from .codec import encode
from .messages import PowerCommand, PowerState

__all__ = [
    "PowerCommand",
    "PowerState",
    "encode",
]
