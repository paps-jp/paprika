"""Operator-run processes that reuse hub internals but are NOT the hub.

Each module here is a ``python -m server.hub.tools.<name>`` entrypoint meant to run
in its own container next to a hub (same image, same env file), so it never
competes with the hub's event loop and survives ``docker restart`` of the hub.
"""
