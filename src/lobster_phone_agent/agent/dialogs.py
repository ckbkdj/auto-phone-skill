from __future__ import annotations

from dataclasses import dataclass

from lobster_phone_agent.device.base import DeviceAdapter
from lobster_phone_agent.device.matcher import SemanticMatcher
from lobster_phone_agent.device.ui import ScreenSnapshot
from lobster_phone_agent.schemas import SemanticTarget
from lobster_phone_agent.util.text import compact_text


@dataclass(slots=True, frozen=True)
class BlockingSurface:
    code: str
    reason: str


class CommonDialogHandler:
    """Handles low-risk system chrome locally and detects surfaces requiring a human."""

    _handoff_markers = {
        "请输入验证码": ("otp_required", "需要用户输入短信或动态验证码"),
        "获取验证码": ("otp_required", "需要用户完成验证码流程"),
        "图形验证码": ("captcha_required", "需要用户完成人机验证"),
        "滑动验证": ("captcha_required", "需要用户完成滑动验证"),
        "安全验证": ("captcha_required", "需要用户完成安全验证"),
        "请输入密码": ("password_required", "需要用户输入密码"),
        "支付密码": ("payment_password_required", "需要用户输入支付密码"),
        "刷脸": ("biometric_required", "需要用户完成人脸识别"),
        "人脸识别": ("biometric_required", "需要用户完成人脸识别"),
        "指纹": ("biometric_required", "需要用户完成指纹验证"),
        "登录": ("login_required", "应用需要用户登录或授权账号"),
    }

    _safe_buttons: tuple[SemanticTarget, ...] = (
        SemanticTarget(
            text="仅在使用中允许",
            aliases=["使用App时允许", "使用应用时允许", "While using the app"],
            role="button",
        ),
        SemanticTarget(
            text="允许",
            aliases=["ALLOW", "Allow", "继续允许"],
            role="button",
        ),
        SemanticTarget(
            text="稍后",
            aliases=["以后再说", "暂不更新", "Not now", "Later"],
            role="button",
        ),
        SemanticTarget(
            text="跳过",
            aliases=["Skip", "暂时跳过", "以后再说"],
            role="button",
        ),
        SemanticTarget(
            text="关闭",
            aliases=["取消", "知道了", "我知道了", "Close"],
            role="button",
        ),
    )

    def __init__(self, matcher: SemanticMatcher) -> None:
        self.matcher = matcher

    def detect_blocking(self, snapshot: ScreenSnapshot) -> BlockingSurface | None:
        labels = compact_text(" ".join(snapshot.labels()))
        # Login alone is too broad. Require another account-related marker.
        if "登录" in labels and any(
            marker in labels for marker in ("手机号", "账号", "密码", "验证码", "注册")
        ):
            return BlockingSurface("login_required", "应用需要用户登录或授权账号")
        for marker, (code, reason) in self._handoff_markers.items():
            if marker == "登录":
                continue
            if compact_text(marker) in labels:
                return BlockingSurface(code, reason)
        return None

    async def dismiss_safe(
        self,
        device: DeviceAdapter,
        snapshot: ScreenSnapshot,
        *,
        operation_id: str | None = None,
    ) -> bool:
        system_like = any(
            marker in snapshot.package.lower()
            for marker in (
                "permissioncontroller",
                "packageinstaller",
                "android.systemui",
                "settings",
            )
        )
        for target in self._safe_buttons:
            match = self.matcher.best(snapshot, target)
            if not match or not match.node.bounds:
                continue
            label = compact_text(match.node.label)
            if label in {compact_text("允许"), compact_text("Allow")} and not system_like:
                # Do not blindly accept an application's own privacy or marketing dialog.
                continue
            x, y = match.node.bounds.center
            await device.tap(x, y, operation_id=operation_id)
            return True
        return False
