"""Homie-native video learning.

The package owns extraction, strategy synthesis, durable sourced notes, and
approval-gated application.  Channel adapters stay thin and call the service.
"""

from .models import VideoLearningRequest, VideoLearningResult
from .service import VideoLearningService, get_video_learning_service

__all__ = [
    "VideoLearningRequest",
    "VideoLearningResult",
    "VideoLearningService",
    "get_video_learning_service",
]
