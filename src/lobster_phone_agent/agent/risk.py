from __future__ import annotations

from dataclasses import dataclass

from lobster_phone_agent.schemas import (
    ActionStep,
    ActionType,
    ConfirmationMode,
    ExecutionPolicy,
    RiskLevel,
)
from lobster_phone_agent.util.text import compact_text

_BLOCKED_MARKERS = {
    "验证码",
    "短信验证码",
    "动态口令",
    "支付密码",
    "登录密码",
    "密码",
    "指纹",
    "人脸识别",
    "刷脸",
    "captcha",
    "verificationcode",
    "otp",
    "password",
    "biometric",
}

_PAYMENT_MARKERS = {
    "支付",
    "付款",
    "确认付款",
    "立即支付",
    "paynow",
    "checkout",
    "purchase",
}

_PURCHASE_MARKERS = {
    "提交订单",
    "确认下单",
    "购买",
    "立即购买",
    "下单",
    "预订",
    "确认预订",
    "booknow",
    "placeorder",
}

_MESSAGE_MARKERS = {
    "发送",
    "发消息",
    "评论",
    "发布",
    "投稿",
    "send",
    "post",
    "publish",
}

_IRREVERSIBLE_MARKERS = {
    "删除",
    "注销",
    "退出登录",
    "取消订单",
    "确认呼叫",
    "立即叫车",
    "呼叫",
    "确认用车",
    "delete",
    "remove",
    "confirmride",
}


@dataclass(slots=True, frozen=True)
class RiskDecision:
    risk: RiskLevel
    confirmation_required: bool
    blocked: bool
    reason: str


class RiskEngine:
    """Fail-closed policy for steps that can create cost or external side effects."""

    def assess(self, step: ActionStep, policy: ExecutionPolicy) -> RiskDecision:
        if step.action in {ActionType.WAIT, ActionType.ASSERT, ActionType.FINISH}:
            return RiskDecision(
                risk=step.risk,
                confirmation_required=False,
                blocked=False,
                reason=step.description or step.action.value,
            )

        text = compact_text(
            " ".join(
                value
                for value in (
                    step.description,
                    step.confirmation_text or "",
                    step.target.text if step.target else "",
                    " ".join(step.target.aliases) if step.target else "",
                    str(step.value or ""),
                )
                if value
            )
        )

        inferred = step.risk
        reason = step.description or step.action.value

        if any(marker in text for marker in _BLOCKED_MARKERS):
            return RiskDecision(
                risk=RiskLevel.BLOCKED,
                confirmation_required=False,
                blocked=True,
                reason="密码、验证码或生物识别必须由用户接管完成",
            )

        if any(marker in text for marker in _PAYMENT_MARKERS):
            inferred = RiskLevel.HIGH
            if not policy.allow_payments:
                return RiskDecision(
                    risk=inferred,
                    confirmation_required=False,
                    blocked=True,
                    reason="策略未授权支付动作",
                )
        elif any(marker in text for marker in _PURCHASE_MARKERS):
            inferred = max(inferred, RiskLevel.HIGH, key=self._rank)
            if not policy.allow_purchases:
                return RiskDecision(
                    risk=inferred,
                    confirmation_required=False,
                    blocked=True,
                    reason="策略未授权购买或下单动作",
                )
        elif any(marker in text for marker in _MESSAGE_MARKERS):
            inferred = max(inferred, RiskLevel.HIGH, key=self._rank)
            if not policy.allow_messages:
                return RiskDecision(
                    risk=inferred,
                    confirmation_required=False,
                    blocked=True,
                    reason="策略未授权对外发送、评论或发布内容",
                )
        elif any(marker in text for marker in _IRREVERSIBLE_MARKERS):
            inferred = max(inferred, RiskLevel.HIGH, key=self._rank)
            if not policy.allow_irreversible:
                return RiskDecision(
                    risk=inferred,
                    confirmation_required=False,
                    blocked=True,
                    reason="策略未授权不可逆动作或叫车提交",
                )
            reason = "动作可能产生费用或不可逆副作用"

        if inferred is RiskLevel.BLOCKED:
            return RiskDecision(
                risk=inferred,
                confirmation_required=False,
                blocked=True,
                reason="该动作被安全策略禁止",
            )

        if inferred in policy.preauthorized_risks:
            return RiskDecision(
                risk=inferred,
                confirmation_required=False,
                blocked=False,
                reason="调用方已预授权该风险级别",
            )

        if policy.confirmation_mode is ConfirmationMode.EVERY_MUTATION:
            mutating = step.action in {
                ActionType.TAP,
                ActionType.TYPE,
                ActionType.CLEAR,
                ActionType.SWIPE,
                ActionType.LAUNCH_APP,
                ActionType.BACK,
                ActionType.HOME,
            }
            return RiskDecision(
                risk=inferred,
                confirmation_required=mutating,
                blocked=False,
                reason=reason,
            )

        confirmation_required = (
            policy.confirmation_mode is ConfirmationMode.RISK_BASED
            and inferred in {RiskLevel.HIGH}
        )
        return RiskDecision(
            risk=inferred,
            confirmation_required=confirmation_required,
            blocked=False,
            reason=reason,
        )

    @staticmethod
    def _rank(value: RiskLevel) -> int:
        return {
            RiskLevel.LOW: 0,
            RiskLevel.MEDIUM: 1,
            RiskLevel.HIGH: 2,
            RiskLevel.BLOCKED: 3,
        }[value]
