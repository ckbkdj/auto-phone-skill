from __future__ import annotations

import asyncio
from typing import Any, Protocol

from lobster_phone_agent.device.ui import ScreenSnapshot, parse_uiautomator_xml
from lobster_phone_agent.errors import ExecutionError


class AppiumSettingsLike(Protocol):
    appium_wait_for_idle_timeout_ms: int
    appium_wait_for_selector_timeout_ms: int
    appium_enable_multi_windows: bool
    appium_command_timeout_seconds: float
    appium_session_ttl_seconds: int


class LocalDescriptorLike(Protocol):
    id: str
    appium_url: str | None
    udid: str | None
    device_name: str | None
    platform_version: str | None
    system_port: int | None
    capabilities: dict[str, Any]


class AppiumDevice:
    """Private-skill Appium adapter.

    This class must never be constructed by the public Docker control plane. The private skill
    owns the Appium URL, UDID, capabilities, and live WebDriver session.
    """

    def __init__(
        self, descriptor: LocalDescriptorLike, driver: Any, settings: AppiumSettingsLike
    ) -> None:
        self.descriptor = descriptor
        self.device_id = descriptor.id
        self._driver = driver
        self._settings = settings
        self._command_lock = asyncio.Lock()

    @staticmethod
    def build_capabilities(
        descriptor: LocalDescriptorLike,
        settings: AppiumSettingsLike,
    ) -> dict[str, Any]:
        capabilities: dict[str, Any] = {
            "platformName": "Android",
            "appium:automationName": "UiAutomator2",
            "appium:deviceName": descriptor.device_name or descriptor.udid or descriptor.id,
            "appium:noReset": True,
            "appium:newCommandTimeout": max(60, settings.appium_session_ttl_seconds),
            "appium:disableWindowAnimation": True,
        }
        if descriptor.udid:
            capabilities["appium:udid"] = descriptor.udid
        if descriptor.platform_version:
            capabilities["appium:platformVersion"] = descriptor.platform_version
        if descriptor.system_port:
            capabilities["appium:systemPort"] = descriptor.system_port
        capabilities.update(descriptor.capabilities)
        # Unstable startup shortcuts and Appium settings are intentionally prohibited here.
        # Settings are applied with update_settings after the session is established.
        capabilities.pop("appium:skipUnlock", None)
        capabilities.pop("skipUnlock", None)
        capabilities.pop("appium:settings", None)
        capabilities.pop("settings", None)
        return capabilities

    @classmethod
    async def connect(
        cls,
        descriptor: LocalDescriptorLike,
        settings: AppiumSettingsLike,
    ) -> AppiumDevice:
        capabilities = cls.build_capabilities(descriptor, settings)

        try:
            from appium import webdriver
            from appium.options.android import UiAutomator2Options
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Appium-Python-Client is not installed in the private skill; "
                "run `pip install 'lobster-phone-agent[skill]'`"
            ) from exc

        options = UiAutomator2Options().load_capabilities(capabilities)
        appium_url = descriptor.appium_url
        if not appium_url:
            raise ExecutionError(f"device {descriptor.id} has no private appium_url")
        try:
            driver = await asyncio.wait_for(
                asyncio.to_thread(
                    webdriver.Remote,
                    command_executor=appium_url,
                    options=options,
                ),
                timeout=settings.appium_command_timeout_seconds,
            )
        except TimeoutError as exc:
            raise ExecutionError("timed out while creating the Appium session") from exc
        instance = cls(descriptor=descriptor, driver=driver, settings=settings)
        await instance._configure_session()
        return instance

    async def _configure_session(self) -> None:
        settings = {
            "waitForIdleTimeout": self._settings.appium_wait_for_idle_timeout_ms,
            "waitForSelectorTimeout": self._settings.appium_wait_for_selector_timeout_ms,
            "enableMultiWindows": self._settings.appium_enable_multi_windows,
        }

        def _update() -> None:
            self._driver.update_settings(settings)

        try:
            await asyncio.wait_for(
                asyncio.to_thread(_update),
                timeout=self._settings.appium_command_timeout_seconds,
            )
        except Exception as exc:
            await self.close()
            raise ExecutionError(f"failed to configure Appium session settings: {exc}") from exc

    async def _run(self, callback, *, timeout: float | None = None):
        command_timeout = timeout or self._settings.appium_command_timeout_seconds
        async with self._command_lock:
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(callback),
                    timeout=command_timeout,
                )
            except TimeoutError as exc:
                raise ExecutionError("Appium command timed out") from exc

    async def snapshot(self) -> ScreenSnapshot:
        def _capture() -> tuple[str, str, str, tuple[int, int]]:
            return (
                self._safe_current_package(),
                self._safe_current_activity(),
                str(self._driver.page_source),
                self._safe_window_size(),
            )

        package, activity, source, size = await self._run(_capture)
        try:
            return parse_uiautomator_xml(
                source,
                package=package,
                activity=activity,
                window_size=size,
            )
        except Exception as exc:
            # One transient empty/partial hierarchy is common while activities switch.
            await asyncio.sleep(0.08)
            package, activity, source, size = await self._run(_capture)
            try:
                return parse_uiautomator_xml(
                    source,
                    package=package,
                    activity=activity,
                    window_size=size,
                )
            except Exception as retry_exc:
                raise ExecutionError(f"invalid Appium page source: {retry_exc}") from exc

    def _safe_current_package(self) -> str:
        try:
            return str(self._driver.current_package or "")
        except Exception:
            return ""

    def _safe_current_activity(self) -> str:
        try:
            return str(self._driver.current_activity or "")
        except Exception:
            return ""

    def _safe_window_size(self) -> tuple[int, int]:
        try:
            value = self._driver.get_window_size()
            return int(value["width"]), int(value["height"])
        except Exception:
            return 0, 0

    async def launch_app(
        self, package: str, *, operation_id: str | None = None
    ) -> None:
        del operation_id
        await self._run(lambda: self._driver.activate_app(package))
        deadline = asyncio.get_running_loop().time() + 2.5
        observed = ""
        while asyncio.get_running_loop().time() < deadline:
            observed = await self._run(self._safe_current_package, timeout=3.0)
            if observed == package or observed.startswith(f"{package}:"):
                return
            state = await self._run(
                lambda: self._query_app_state(package),
                timeout=3.0,
            )
            # State 4 is foreground. State 3 is accepted only while a known Android system
            # overlay covers the app; accepting any background process produced false launches.
            if state >= 4:
                return
            if state == 3 and self._is_system_overlay_package(observed):
                return
            await asyncio.sleep(0.12)
        raise ExecutionError(
            "activate_app returned but the target package is not running; "
            f"foreground package={observed!r}, target={package!r}"
        )

    def _query_app_state(self, package: str) -> int:
        try:
            value = self._driver.query_app_state(package)
            return int(getattr(value, "value", value))
        except Exception:
            return 0

    @staticmethod
    def _is_system_overlay_package(package: str) -> bool:
        normalized = package.casefold()
        return any(
            marker in normalized
            for marker in (
                "permissioncontroller",
                "packageinstaller",
                "android.systemui",
            )
        )

    async def tap(
        self, x: int, y: int, *, operation_id: str | None = None
    ) -> None:
        del operation_id

        def _tap() -> None:
            result = self._driver.execute_script(
                "mobile: clickGesture",
                {"x": int(x), "y": int(y)},
            )
            if result is False:
                raise ExecutionError("Appium clickGesture reported performed=false")

        await self._run(_tap)

    def _find_element_sync(self, element: dict[str, Any] | None) -> Any | None:
        if not element:
            return None
        try:
            from appium.webdriver.common.appiumby import AppiumBy
        except ModuleNotFoundError:
            return None
        locators: list[tuple[str, str]] = []
        resource_id = str(element.get("resource_id") or "")
        accessibility_id = str(element.get("accessibility_id") or "")
        text = str(element.get("text") or "")
        if resource_id:
            locators.append((AppiumBy.ID, resource_id))
        if accessibility_id:
            locators.append((AppiumBy.ACCESSIBILITY_ID, accessibility_id))
        if text:
            escaped = text.replace("\\", "\\\\").replace('"', '\\"')
            locators.append(
                (AppiumBy.ANDROID_UIAUTOMATOR, f'new UiSelector().text("{escaped}")')
            )
        for by, value in locators:
            try:
                return self._driver.find_element(by, value)
            except Exception:
                continue
        return None

    async def type_text(
        self,
        text: str,
        *,
        clear: bool = False,
        element: dict[str, Any] | None = None,
        operation_id: str | None = None,
    ) -> None:
        del operation_id

        def _type() -> None:
            target = self._find_element_sync(element)
            if target is None:
                target = self._driver.switch_to.active_element
            try:
                target.click()
            except Exception:
                pass

            failures: list[str] = []

            def clear_for_retry() -> None:
                if not clear:
                    return
                try:
                    target.clear()
                except Exception as exc:
                    failures.append(f"clear={exc}")

            # replaceElementValue is a replacement API, so it is only valid for clear-first
            # semantics. Using it for append input silently overwrote existing text.
            element_id = getattr(target, "id", None)
            if clear and element_id:
                clear_for_retry()
                try:
                    self._driver.execute_script(
                        "mobile: replaceElementValue",
                        {"elementId": element_id, "text": text},
                    )
                    if self._element_matches(target, text, exact=True):
                        return
                    failures.append("replaceElementValue=verification failed")
                except Exception as exc:
                    failures.append(f"replaceElementValue={exc}")

            clear_for_retry()
            try:
                target.send_keys(text)
                if self._element_matches(target, text, exact=clear):
                    return
                failures.append("send_keys=verification failed")
            except Exception as exc:
                failures.append(f"send_keys={exc}")

            clear_for_retry()
            try:
                self._driver.execute_script("mobile: type", {"text": text})
                if self._element_matches(
                    target,
                    text,
                    exact=clear,
                    allow_unreadable=True,
                ):
                    return
                failures.append("mobile:type=verification failed")
            except Exception as exc:
                failures.append(f"mobile:type={exc}")
            raise ExecutionError("text input was not verified: " + "; ".join(failures[-5:]))

        await self._run(_type)

    @staticmethod
    def _element_values(target: Any) -> list[str]:
        values: list[str] = []
        for name in ("text",):
            try:
                value = getattr(target, name)
                if value is not None:
                    values.append(str(value))
            except Exception:
                pass
        for name in ("text", "value"):
            try:
                value = target.get_attribute(name)
                if value is not None:
                    values.append(str(value))
            except Exception:
                pass
        return values

    @classmethod
    def _element_matches(
        cls,
        target: Any,
        text: str,
        *,
        exact: bool,
        allow_unreadable: bool = False,
    ) -> bool:
        values = cls._element_values(target)
        if not values:
            return allow_unreadable
        if exact:
            return any(value == text for value in values)
        return any(text in value for value in values if value)

    @classmethod
    def _element_empty(cls, target: Any, *, allow_unreadable: bool = False) -> bool:
        values = cls._element_values(target)
        if not values:
            return allow_unreadable
        return all(not value for value in values)

    async def clear_active(
        self,
        *,
        element: dict[str, Any] | None = None,
        operation_id: str | None = None,
    ) -> None:
        del operation_id

        def _clear() -> None:
            target = self._find_element_sync(element) or self._driver.switch_to.active_element
            target.clear()
            if not self._element_empty(target, allow_unreadable=True):
                raise ExecutionError("clear command was not verified")

        await self._run(_clear)

    async def swipe(
        self,
        direction: str,
        *,
        percent: float = 0.72,
        operation_id: str | None = None,
    ) -> None:
        del operation_id

        def _swipe() -> None:
            width, height = self._safe_window_size()
            if width <= 0 or height <= 0:
                raise ExecutionError("invalid Appium window size")
            left = max(1, int(width * 0.08))
            top = max(1, int(height * 0.12))
            gesture_width = max(20, int(width * 0.84))
            gesture_height = max(20, int(height * 0.72))
            result = self._driver.execute_script(
                "mobile: swipeGesture",
                {
                    "left": left,
                    "top": top,
                    "width": gesture_width,
                    "height": gesture_height,
                    "direction": direction,
                    "percent": percent,
                },
            )
            if result is False:
                raise ExecutionError("Appium swipeGesture reported performed=false")

        await self._run(_swipe)

    async def back(self, *, operation_id: str | None = None) -> None:
        del operation_id
        await self._run(self._driver.back)

    async def home(self, *, operation_id: str | None = None) -> None:
        del operation_id

        def _home() -> None:
            result = self._driver.execute_script("mobile: pressKey", {"keycode": 3})
            if result is False:
                raise ExecutionError("Appium pressKey reported performed=false")

        await self._run(_home)

    async def list_apps(self) -> list[dict[str, object]]:
        def _list() -> list[dict[str, object]]:
            # Current UiAutomator2 uses the `user` argument; `type=user` is invalid.
            result = self._driver.execute_script("mobile: listApps", {"user": "current"})
            if isinstance(result, list):
                normalized: list[dict[str, object]] = []
                for item in result:
                    if isinstance(item, dict):
                        normalized.append(item)
                    elif isinstance(item, str):
                        normalized.append({"package": item})
                return normalized
            if isinstance(result, dict):
                return [{"package": key, "details": value} for key, value in result.items()]
            raise ExecutionError(f"unexpected mobile:listApps response: {type(result).__name__}")

        return await self._run(_list)

    async def screenshot_png(self) -> bytes:
        return bytes(await self._run(self._driver.get_screenshot_as_png))

    async def is_alive(self) -> bool:
        try:
            session_id = await self._run(lambda: self._driver.session_id, timeout=3.0)
            await self._run(self._driver.get_window_size, timeout=3.0)
            return bool(session_id)
        except Exception:
            return False

    async def close(self) -> None:
        try:
            await asyncio.wait_for(asyncio.to_thread(self._driver.quit), timeout=5.0)
        except Exception:
            pass
