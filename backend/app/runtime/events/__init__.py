"""Event Bus - typed publish/subscribe channel, single source of truth."""

from app.runtime.events.backpressure import (
    DEFAULT_LIFECYCLE_TIMEOUT,
    DEFAULT_MAX_DEPTH,
    BackpressureQueue,
    is_coalescible,
    is_lifecycle_event,
)
from app.runtime.events.bus import EventBus, Subscriber, SubscriberHandler
from app.runtime.events.models import DecisionKind, DecisionRecord, Event, EventType

__all__ = [
    "BackpressureQueue",
    "DEFAULT_LIFECYCLE_TIMEOUT",
    "DEFAULT_MAX_DEPTH",
    "DecisionKind",
    "DecisionRecord",
    "Event",
    "EventBus",
    "EventType",
    "Subscriber",
    "SubscriberHandler",
    "is_coalescible",
    "is_lifecycle_event",
]
