from lobster_phone_agent.device.matcher import SemanticMatcher
from lobster_phone_agent.schemas import SemanticTarget

from .fakes import screen


def test_matcher_prefers_exact_resource_id_and_text() -> None:
    snapshot = screen(
        "com.sankuai.meituan",
        {
            "text": "附近优惠",
            "resource_id": "com.sankuai.meituan:id/title",
            "bounds": "[20,100][500,180]",
        },
        {
            "text": "打车",
            "resource_id": "com.sankuai.meituan:id/ride_entry",
            "class_name": "android.widget.Button",
            "bounds": "[20,300][260,430]",
        },
    )
    target = SemanticTarget(text="打车", aliases=["美团打车"], role="button")
    match = SemanticMatcher().best(snapshot, target)
    assert match is not None
    assert match.node.text == "打车"
    assert match.score > 8


def test_screen_compaction_does_not_expose_password_text() -> None:
    snapshot = screen(
        "com.example",
        {
            "text": "super-secret",
            "password": True,
            "class_name": "android.widget.EditText",
            "focusable": True,
        },
    )
    compact = snapshot.compact()
    assert "super-secret" not in compact
    assert "<password>" in compact


def test_matcher_rejects_duplicate_exact_text_without_stable_locator() -> None:
    snapshot = screen(
        "com.example",
        {"text": "确定", "class_name": "android.widget.Button", "bounds": "[10,10][300,120]"},
        {"text": "确定", "class_name": "android.widget.Button", "bounds": "[400,10][700,120]"},
    )
    matcher = SemanticMatcher()
    assert matcher.best(snapshot, SemanticTarget(text="确定", role="button")) is None
    selected = matcher.best(
        snapshot,
        SemanticTarget(text="确定", role="button", index=1),
    )
    assert selected is not None
    assert selected.node.bounds and selected.node.bounds.x1 == 400


def test_labeled_child_is_promoted_to_clickable_ancestor() -> None:
    from lobster_phone_agent.device.ui import parse_uiautomator_xml
    from lobster_phone_agent.schemas import ActionType

    xml = """<hierarchy>
      <node class="android.view.ViewGroup" clickable="true" enabled="true" displayed="true"
            bounds="[0,0][500,180]">
        <node class="android.widget.TextView" text="立即叫车" clickable="false"
              enabled="true" displayed="true" bounds="[20,20][300,120]" />
      </node>
    </hierarchy>"""
    snapshot = parse_uiautomator_xml(
        xml,
        package="com.example",
        activity=".Main",
        window_size=(1080, 2400),
    )
    matcher = SemanticMatcher()
    matched = matcher.best(snapshot, SemanticTarget(text="立即叫车", role="button"))
    assert matched is not None
    interaction = matcher.interaction_node(snapshot, matched, ActionType.TAP)
    assert interaction.node.clickable
    assert interaction.node.path == "0/0"


def test_empty_content_description_never_matches_arbitrary_target() -> None:
    matcher = SemanticMatcher()
    snapshot = screen("com.example", {"text": "列表顶部", "content_desc": ""})

    assert matcher.best(snapshot, SemanticTarget(text="继续", role="button")) is None


def test_empty_text_never_matches_arbitrary_target_by_reverse_contains() -> None:
    matcher = SemanticMatcher()
    snapshot = screen(
        "com.example",
        {"text": "", "content_desc": "列表顶部", "class_name": "android.widget.Button"},
    )

    assert matcher.best(snapshot, SemanticTarget(text="继续", role="button")) is None
