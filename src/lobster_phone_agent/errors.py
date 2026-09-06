from __future__ import annotations


class PhoneAgentError(Exception):
    """Base exception for errors that are safe to expose through the API."""


class PlanningError(PhoneAgentError):
    pass


class ExecutionError(PhoneAgentError):
    pass


class ElementNotFound(ExecutionError):
    pass


class ConditionTimeout(ExecutionError):
    pass


class TaskNotFound(PhoneAgentError):
    pass


class InvalidTaskState(PhoneAgentError):
    pass


class TaskCancelled(PhoneAgentError):
    pass


class ConfirmationRejected(PhoneAgentError):
    pass


class HandoffRequired(PhoneAgentError):
    def __init__(self, reason: str, *, code: str = "human_action_required") -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code


class BridgeUnavailable(ExecutionError):
    pass


class BridgeProtocolError(ExecutionError):
    pass
