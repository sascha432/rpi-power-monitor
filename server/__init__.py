"""Power-monitor server package (runs on the Raspberry Pi).

Reads an INA3221 power sensor over I2C, computes physical + virtual
(combined) channels, integrates energy, and streams the readings to TCP
clients. Run with ``python -m server`` from the repository root.
"""
