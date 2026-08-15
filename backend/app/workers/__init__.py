"""Celery application and job workers.

Workers are the only processes with a route to the OOB plane and the only ones
that ever hold BMC credentials.
"""
