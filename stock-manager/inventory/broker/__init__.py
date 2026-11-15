"""The freight broker: one engine that controls both when orders leave and which vehicle carries them.

Run it with `python -m inventory.broker run` (see `--help`). It pulls real towns, addresses,
and road distances from French open data, builds client firms whose demand
follows the sample year, and plays the same weeks three ways (solo, pooled, broker) to compare.
"""
