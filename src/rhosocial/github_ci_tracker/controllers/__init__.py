# src/rhosocial/github_ci_tracker/controllers/__init__.py
"""The controller layer: one module per group of flows.

A controller is a **group of flows**, not a table gateway and not a use case
buried in a protocol adapter. "Trigger this workflow and wait for it",
"subscribe this repository", "apply this delivery" are flows: each one is a
sequence of reads, calls to a service and writes, and each one is written once
here regardless of how many protocols expose it.

Two kinds of class live in these modules, and the difference is deliberate:

* the flow classes below -- what a protocol adapter calls;
* the row classes (``RunData``, ``RepositoryData``, ``DeliveryLedger``) -- the
  model operations those flows are assembled from, kept beside the flow that
  owns them so no caller re-derives the order. They are the only place that
  builds a query.

A flow talks to a service to reach the outside world and to a row class to reach
the database. The model decides what a payload means; the flow decides when to
ask. Nothing in this package imports a protocol adapter, and no adapter imports
from here except to name a type.
"""

from __future__ import annotations

from .dispatch import DispatchController
from .logs import RunLogCapture
from .query import QueryController
from .repository import RepositoryController, RepositoryData
from .run import RunController, RunData
from .status import StatusController
from .token import TokenController
from .trigger import TriggerController
from .webhook import DeliveryLedger, WebhookController
from .workflow import WorkflowController

__all__ = [
    "DispatchController",
    "QueryController",
    "RepositoryController",
    "RepositoryData",
    "RunController",
    "RunLogCapture",
    "RunData",
    "StatusController",
    "TokenController",
    "TriggerController",
    "WebhookController",
    "DeliveryLedger",
    "WorkflowController",
]
