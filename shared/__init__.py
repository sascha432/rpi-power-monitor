"""Shared data model and wire-protocol definitions for rpi-power-monitor.

This package holds ONLY the contract between the server and the GUI client
(data structures, framing, message types). It must stay dependency-free so
both sides can import it.
"""
