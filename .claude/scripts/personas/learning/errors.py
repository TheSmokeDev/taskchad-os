"""Typed learning control signals shared by cognition, hooks and evaluation."""


class LearningDeferredError(RuntimeError):
    """Work remains pending because pause, foreground work or a lease takes priority."""


# Preserve the original public import and the actual exception identity.
LearningDeferred = LearningDeferredError


class LearningUnavailableError(LearningDeferredError):
    """Required inference or source infrastructure is unavailable, not disproven."""


class LearningOutputError(LearningDeferredError):
    """Inference returned an invalid contract; retry without rejecting a candidate."""
