from lobster_phone_agent.agent.selector_memory import SelectorMemory
from lobster_phone_agent.device.matcher import SemanticMatcher
from lobster_phone_agent.edge.router import LocalIntentRouter
from lobster_phone_agent.schemas import SemanticTarget

from .fakes import screen


def test_local_router_recognizes_ride_and_launch() -> None:
    router = LocalIntentRouter()
    assert router.route("帮我用美团打车去机场").intent == "ride_hailing"
    assert router.route("打开微信").intent == "launch_app"


def test_selector_memory_is_bound_to_screen_fingerprint() -> None:
    target = SemanticTarget(text="搜索", role="button")
    first = screen(
        "com.example",
        {"text": "搜索", "class_name": "android.widget.Button"},
    )
    changed = screen(
        "com.example",
        {"text": "其他", "class_name": "android.widget.Button"},
    )
    match = SemanticMatcher().best(first, target)
    assert match is not None
    memory = SelectorMemory()
    memory.remember(first, target, match)
    assert memory.recall(first, target) is not None
    assert memory.recall(changed, target) is None
