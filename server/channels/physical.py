"""Physical channel model.

Each physical channel corresponds to one INA3221 input (CH1..CH3) and is
described by a config ``ShuntChannel`` (name + shunt resistance). A driver
``RawReading`` for the matching input is turned into the reading fields
``voltage_v`` / ``current_a`` / ``power_w`` (+ accumulated ``energy_wh``).

TODO(server): implement a thin wrapper that, given a RawReading and the
running energy counter for the channel, produces ``{field: value}``.
"""
