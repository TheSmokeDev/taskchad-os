"""Persona-owned, provider-independent learning contracts.

Importing this package performs no profile lookup, filesystem writes or model calls.
"""

from personas.learning.models import LearningContext, LearningError, LearningTarget

__all__ = ["LearningContext", "LearningError", "LearningTarget"]
