"""Virtual (combined) channel computation.

Supported kinds (see config/server.yaml -> virtual_channels):
  - "sum":  power / current / energy are summed over the member channels;
            voltage is omitted (not well defined for a parallel sum).
  - "mean": arithmetic mean of one chosen field over the members.

TODO(server): implement ``VirtualChannel.evaluate(channel_values)`` returning
the ``{field: value}`` dict for this virtual channel.
"""
