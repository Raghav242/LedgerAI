from app.core.exceptions import DomainError


class LabRunInProgressError(DomainError):
    """Another run is queued or running; experiments never overlap."""

    code = "LAB_RUN_IN_PROGRESS"


class LabRunNotFoundError(DomainError):
    code = "LAB_RUN_NOT_FOUND"
