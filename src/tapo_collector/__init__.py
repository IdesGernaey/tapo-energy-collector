"""UGent Racing Tapo P110 energy collector.

Reads TP-Link Tapo P110 smart plugs on the local network, buffers every reading in a
local SQLite queue and uploads the readings to the CCBOM website over HTTPS.
The collector only *reads* the plugs: it never switches them on or off.
"""

__version__ = "1.1.0"
