from lobster_phone_agent.agent.risk import RiskEngine
from lobster_phone_agent.schemas import (
    ActionStep,
    ActionType,
    ExecutionPolicy,
    RiskLevel,
    SemanticTarget,
)


def test_ride_submission_requires_confirmation() -> None:
    step = ActionStep(
        action=ActionType.TAP,
        description="确认呼叫车辆",
        target=SemanticTarget(text="确认呼叫"),
        risk=RiskLevel.HIGH,
    )
    blocked = RiskEngine().assess(step, ExecutionPolicy())
    assert blocked.blocked
    decision = RiskEngine().assess(step, ExecutionPolicy(allow_irreversible=True))
    assert decision.confirmation_required
    assert not decision.blocked


def test_password_surface_is_blocked_for_handoff() -> None:
    step = ActionStep(
        action=ActionType.TYPE,
        description="输入支付密码",
        target=SemanticTarget(text="支付密码", role="input"),
        value="123456",
    )
    decision = RiskEngine().assess(step, ExecutionPolicy())
    assert decision.blocked
    assert decision.risk is RiskLevel.BLOCKED


def test_payment_requires_explicit_policy_permission() -> None:
    step = ActionStep(
        action=ActionType.TAP,
        description="立即支付",
        target=SemanticTarget(text="立即支付", role="button"),
        risk=RiskLevel.HIGH,
    )
    decision = RiskEngine().assess(step, ExecutionPolicy())
    assert decision.blocked
    allowed = RiskEngine().assess(step, ExecutionPolicy(allow_payments=True))
    assert not allowed.blocked
    assert allowed.confirmation_required
