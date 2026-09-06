from __future__ import annotations

from types import SimpleNamespace

import pytest

from lobster_phone_agent.device.appium_device import AppiumDevice
from lobster_phone_agent.errors import ExecutionError
from lobster_phone_agent.skill.models import LocalDeviceDescriptor, SkillRuntimeConfig


class FakeElement:
    def __init__(self, text: str = "") -> None:
        self.id = "element-1"
        self.text = text
        self.clicked = 0

    def click(self) -> None:
        self.clicked += 1

    def clear(self) -> None:
        self.text = ""

    def send_keys(self, text: str) -> None:
        self.text += text

    def get_attribute(self, name: str):
        if name in {"text", "value"}:
            return self.text
        return None


class FakeDriver:
    def __init__(self) -> None:
        self.element = FakeElement()
        self.switch_to = SimpleNamespace(active_element=self.element)
        self.scripts: list[tuple[str, dict[str, object]]] = []
        self.settings: dict[str, object] | None = None
        self.session_id = "session-1"
        self.current_package = "com.example"
        self.current_activity = ".Main"
        self.page_source = '<hierarchy><node class="android.widget.TextView" text="ok" bounds="[0,0][10,10]"/></hierarchy>'
        self.click_result: object = True
        self.swipe_result: object = True
        self.press_key_result: object = True

    def update_settings(self, settings: dict[str, object]) -> None:
        self.settings = settings

    def execute_script(self, name: str, params: dict[str, object]):
        self.scripts.append((name, params))
        if name == "mobile: clickGesture":
            return self.click_result
        if name == "mobile: swipeGesture":
            return self.swipe_result
        if name == "mobile: replaceElementValue":
            self.element.text = str(params["text"])
            return None
        if name == "mobile: type":
            self.element.text += str(params["text"])
            return None
        if name == "mobile: listApps":
            return {"com.example": {"versionName": "1.0"}}
        if name == "mobile: pressKey":
            return self.press_key_result
        raise AssertionError(name)

    def find_element(self, _by: str, _value: str):
        return self.element

    def get_window_size(self) -> dict[str, int]:
        return {"width": 1080, "height": 2400}

    def back(self) -> None:
        return None

    def quit(self) -> None:
        self.session_id = None

    def activate_app(self, package: str) -> None:
        self.current_package = package

    def get_screenshot_as_png(self) -> bytes:
        return b"png"


def descriptor(**capabilities) -> LocalDeviceDescriptor:
    return LocalDeviceDescriptor(
        id="cloud-1",
        appium_url="http://127.0.0.1:4723",
        udid="emulator-5554",
        capabilities=capabilities,
    )


def test_capabilities_reject_protected_session_overrides() -> None:
    with pytest.raises(ValueError, match="protected session fields"):
        descriptor(
            **{
                "appium:skipUnlock": True,
                "skipUnlock": True,
                "appium:settings": {"waitForIdleTimeout": 0},
                "settings": {"waitForIdleTimeout": 0},
            }
        )


def test_capabilities_keep_safe_custom_values_and_private_target() -> None:
    caps = AppiumDevice.build_capabilities(
        descriptor(**{"appium:autoGrantPermissions": True}),
        SkillRuntimeConfig(),
    )
    assert caps["appium:udid"] == "emulator-5554"
    assert caps["appium:autoGrantPermissions"] is True
    assert caps["appium:noReset"] is True


@pytest.mark.asyncio
async def test_session_settings_are_applied_after_connection() -> None:
    driver = FakeDriver()
    settings = SkillRuntimeConfig(
        appium_wait_for_idle_timeout_ms=650,
        appium_wait_for_selector_timeout_ms=1700,
        appium_enable_multi_windows=True,
    )
    device = AppiumDevice(descriptor(), driver, settings)
    await device._configure_session()
    assert driver.settings == {
        "waitForIdleTimeout": 650,
        "waitForSelectorTimeout": 1700,
        "enableMultiWindows": True,
    }


@pytest.mark.asyncio
async def test_false_click_and_swipe_results_are_failures() -> None:
    driver = FakeDriver()
    device = AppiumDevice(descriptor(), driver, SkillRuntimeConfig())
    driver.click_result = False
    with pytest.raises(ExecutionError, match="performed=false"):
        await device.tap(20, 30)
    driver.swipe_result = False
    with pytest.raises(ExecutionError, match="performed=false"):
        await device.swipe("up")


@pytest.mark.asyncio
async def test_false_home_result_is_a_failure() -> None:
    driver = FakeDriver()
    driver.press_key_result = False
    device = AppiumDevice(descriptor(), driver, SkillRuntimeConfig())
    with pytest.raises(ExecutionError, match="performed=false"):
        await device.home()


@pytest.mark.asyncio
async def test_unicode_input_prefers_replace_element_value_and_is_verified() -> None:
    driver = FakeDriver()
    device = AppiumDevice(descriptor(), driver, SkillRuntimeConfig())
    await device.type_text("北京南站", clear=True, element={"resource_id": "destination"})
    assert driver.element.text == "北京南站"
    assert (
        "mobile: replaceElementValue",
        {"elementId": "element-1", "text": "北京南站"},
    ) in driver.scripts


@pytest.mark.asyncio
async def test_clear_and_list_apps_are_verified_with_current_user_argument() -> None:
    driver = FakeDriver()
    driver.element.text = "old"
    device = AppiumDevice(descriptor(), driver, SkillRuntimeConfig())
    await device.clear_active(element={"resource_id": "destination"})
    assert driver.element.text == ""
    apps = await device.list_apps()
    assert apps == [{"package": "com.example", "details": {"versionName": "1.0"}}]
    assert ("mobile: listApps", {"user": "current"}) in driver.scripts

class PartialReplaceDriver(FakeDriver):
    def execute_script(self, name: str, params: dict[str, object]):
        if name == "mobile: replaceElementValue":
            self.scripts.append((name, params))
            self.element.text = str(params["text"])[:2]
            raise RuntimeError("IME interrupted replacement")
        return super().execute_script(name, params)


class BrokenElement(FakeElement):
    def send_keys(self, text: str) -> None:
        self.text = text[:2]


class BrokenInputDriver(FakeDriver):
    def __init__(self) -> None:
        super().__init__()
        self.element = BrokenElement()
        self.switch_to = SimpleNamespace(active_element=self.element)

    def execute_script(self, name: str, params: dict[str, object]):
        self.scripts.append((name, params))
        if name in {"mobile: replaceElementValue", "mobile: type"}:
            self.element.text = str(params["text"])[:2]
            return None
        return super().execute_script(name, params)


class PermissionOverlayDriver(FakeDriver):
    def __init__(self) -> None:
        super().__init__()
        self.current_package = "com.google.android.permissioncontroller"
        self.activated_package = ""

    def activate_app(self, package: str) -> None:
        self.activated_package = package

    def query_app_state(self, package: str) -> int:
        assert package == self.activated_package
        return 3


@pytest.mark.asyncio
async def test_clear_input_fallback_clears_partial_text_before_send_keys() -> None:
    driver = PartialReplaceDriver()
    device = AppiumDevice(descriptor(), driver, SkillRuntimeConfig())
    await device.type_text("北京南站", clear=True, element={"resource_id": "destination"})
    assert driver.element.text == "北京南站"


@pytest.mark.asyncio
async def test_append_input_never_uses_replacement_api() -> None:
    driver = FakeDriver()
    driver.element.text = "北京"
    device = AppiumDevice(descriptor(), driver, SkillRuntimeConfig())
    await device.type_text("南站", clear=False, element={"resource_id": "destination"})
    assert driver.element.text == "北京南站"
    assert not any(name == "mobile: replaceElementValue" for name, _ in driver.scripts)


@pytest.mark.asyncio
async def test_clear_input_requires_exact_verified_value() -> None:
    driver = BrokenInputDriver()
    device = AppiumDevice(descriptor(), driver, SkillRuntimeConfig())
    with pytest.raises(ExecutionError, match="text input was not verified"):
        await device.type_text(
            "北京南站",
            clear=True,
            element={"resource_id": "destination"},
        )


@pytest.mark.asyncio
async def test_launch_accepts_running_app_behind_permission_controller() -> None:
    driver = PermissionOverlayDriver()
    device = AppiumDevice(descriptor(), driver, SkillRuntimeConfig())
    await device.launch_app("com.example")
    assert driver.activated_package == "com.example"
