"""Harness-owned durable records and coordination.

The harness decides what runs, when, and whether again. It owns the durable
records of that decision: the queue item that assigns an event to an agent, the
numbered attempt, and the run handle.
"""
