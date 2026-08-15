"""Domain logic: state machines, job orchestration, and boot rendering.

Nothing here imports from `app.api`. Routers call services; services never call
back into HTTP.
"""
