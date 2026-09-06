from __future__ import annotations

from dataclasses import dataclass
from math import hypot

from rapidfuzz.fuzz import ratio

from lobster_phone_agent.device.ui import (
    ScreenSnapshot,
    UiNode,
    iter_ancestors,
    iter_descendants,
)
from lobster_phone_agent.schemas import ActionType, SemanticTarget
from lobster_phone_agent.util.text import compact_text, tokenize


@dataclass(slots=True)
class MatchResult:
    node: UiNode
    score: float
    reason: str
    exact: bool = False


class SemanticMatcher:
    """Fast, conservative local selector.

    A false negative is preferable to a confident click on the wrong control. Ambiguous matches
    are rejected unless the target contains an exact stable locator or an explicit index.
    """

    def __init__(self, minimum_score: float = 4.0, ambiguity_margin: float = 0.65) -> None:
        self.minimum_score = minimum_score
        self.ambiguity_margin = ambiguity_margin

    def rank(self, snapshot: ScreenSnapshot, target: SemanticTarget) -> list[MatchResult]:
        results = []
        for node in snapshot.nodes:
            if not self._eligible(snapshot, node, target):
                continue
            result = self._score(snapshot, node, target)
            if result.score >= self.minimum_score:
                results.append(result)
        results.sort(
            key=lambda result: (
                result.score,
                result.exact,
                result.node.clickable,
                result.node.enabled,
                -(result.node.bounds.area if result.node.bounds else 10**9),
                -result.node.depth,
            ),
            reverse=True,
        )
        if target.index is not None:
            return results[target.index : target.index + 1]
        return results

    def best(self, snapshot: ScreenSnapshot, target: SemanticTarget) -> MatchResult | None:
        ranked = self.rank(snapshot, target)
        if not ranked:
            return None
        top = ranked[0]
        if len(ranked) == 1:
            return top
        # Text equality is not a stable locator: two identical "确定" buttons must not be
        # resolved by tree order. Only an exact resource/accessibility locator may bypass the
        # ambiguity margin (an explicit target.index is already handled by rank()).
        if top.exact and (target.resource_id or target.accessibility_id):
            return top
        second = ranked[1]
        if top.score - second.score < self.ambiguity_margin:
            return None
        return top

    def interaction_node(
        self,
        snapshot: ScreenSnapshot,
        match: MatchResult,
        action: ActionType,
    ) -> MatchResult | None:
        node = match.node
        if action is ActionType.TAP:
            if node.clickable:
                return match
            for ancestor in iter_ancestors(node, snapshot.nodes):
                if not (
                    ancestor.displayed
                    and ancestor.enabled
                    and ancestor.clickable
                    and ancestor.bounds is not None
                    and ancestor.bounds.intersects(snapshot.width, snapshot.height)
                ):
                    continue
                viewport_area = max(1, snapshot.width * snapshot.height)
                if ancestor.bounds.area > viewport_area * 0.8:
                    continue
                return MatchResult(
                    node=ancestor,
                    score=match.score + 0.25,
                    reason=f"{match.reason}, clickable-ancestor",
                    exact=match.exact,
                )
            return None
        if action in {ActionType.TYPE, ActionType.CLEAR}:
            if node.editable:
                return match
            for candidate in (
                *iter_ancestors(node, snapshot.nodes),
                *iter_descendants(node, snapshot.nodes),
            ):
                if (
                    candidate.displayed
                    and candidate.enabled
                    and candidate.editable
                    and candidate.bounds is not None
                    and candidate.bounds.intersects(snapshot.width, snapshot.height)
                ):
                    return MatchResult(
                        node=candidate,
                        score=match.score + 0.25,
                        reason=f"{match.reason}, editable-relative",
                        exact=match.exact,
                    )
            return None
        return match

    @staticmethod
    def _eligible(snapshot: ScreenSnapshot, node: UiNode, target: SemanticTarget) -> bool:
        if not node.displayed or not node.enabled or node.password:
            return False
        if node.bounds is None or not node.bounds.intersects(snapshot.width, snapshot.height):
            return False
        role = (target.role or "").lower()
        ancestors = tuple(iter_ancestors(node, snapshot.nodes))
        descendants = tuple(iter_descendants(node, snapshot.nodes))
        if role == "input" and not node.editable:
            candidates = (*ancestors, *descendants)
            if not any(
                candidate.displayed and candidate.enabled and candidate.editable
                for candidate in candidates
            ):
                return False
        if role in {"button", "checkbox", "radio", "switch"}:
            if node.role != role and not node.clickable:
                if not any(
                    candidate.displayed and candidate.enabled and candidate.clickable
                    for candidate in ancestors
                ):
                    return False
        return True

    def _score(
        self,
        snapshot: ScreenSnapshot,
        node: UiNode,
        target: SemanticTarget,
    ) -> MatchResult:
        score = 0.0
        reasons: list[str] = []
        exact = False
        node_text = compact_text(node.text)
        node_desc = compact_text(node.content_desc)
        node_id = node.resource_id.lower()
        labels = [compact_text(label) for label in target.labels() if compact_text(label)]

        if target.package:
            if node.package == target.package:
                score += 1.5
                reasons.append("package")
            elif node.package and target.package not in node.package:
                return MatchResult(node=node, score=-100.0, reason="package-mismatch")

        if target.resource_id:
            wanted = target.resource_id.lower()
            if node_id == wanted:
                score += 12.0
                exact = True
                reasons.append("resource-id exact")
            elif node_id.endswith(wanted) or (target.allow_partial and wanted in node_id):
                score += 7.0
                reasons.append("resource-id partial")
            else:
                return MatchResult(node=node, score=-100.0, reason="resource-id-mismatch")

        if target.accessibility_id:
            wanted = compact_text(target.accessibility_id)
            if node_desc == wanted:
                score += 10.0
                exact = True
                reasons.append("accessibility exact")
            elif target.allow_partial and wanted and wanted in node_desc:
                score += 6.0
                reasons.append("accessibility partial")
            else:
                return MatchResult(node=node, score=-100.0, reason="accessibility-mismatch")

        best_label_score = 0.0
        best_label_reason = ""
        best_label_exact = False
        for label in labels:
            candidate_scores: list[tuple[float, str, bool]] = []
            if node_text == label:
                candidate_scores.append((9.5, "text exact", True))
            if node_desc == label:
                candidate_scores.append((9.0, "description exact", True))
            if (
                label
                and node_text
                and target.allow_partial
                and (label in node_text or node_text in label)
            ):
                candidate_scores.append((6.2, "text contains", False))
            if (
                label
                and node_desc
                and target.allow_partial
                and (label in node_desc or node_desc in label)
            ):
                candidate_scores.append((5.8, "description contains", False))
            for candidate in (node_text, node_desc, compact_text(node.resource_id)):
                if not candidate:
                    continue
                fuzzy = ratio(label, candidate) / 100.0
                overlap = len(tokenize(label) & tokenize(candidate))
                candidate_scores.append(
                    (fuzzy * 3.4 + min(overlap, 3) * 0.35, f"fuzzy={fuzzy:.2f}", False)
                )
            if candidate_scores:
                current_score, current_reason, current_exact = max(
                    candidate_scores, key=lambda item: item[0]
                )
                if current_score > best_label_score:
                    best_label_score = current_score
                    best_label_reason = current_reason
                    best_label_exact = current_exact
        score += best_label_score
        exact = exact or best_label_exact
        if best_label_reason:
            reasons.append(best_label_reason)

        if labels and best_label_score <= 0:
            return MatchResult(node=node, score=-100.0, reason="label-mismatch")

        if target.role:
            if node.role == target.role.lower():
                score += 1.5
                reasons.append("role")
            elif target.role.lower() == "input" and node.editable:
                score += 1.0
                reasons.append("editable")
            else:
                score -= 1.5

        if target.near_text and node.bounds:
            near = compact_text(target.near_text)
            nearest_distance: float | None = None
            for candidate in snapshot.nodes:
                if not candidate.bounds:
                    continue
                labels_to_check = (
                    compact_text(candidate.text),
                    compact_text(candidate.content_desc),
                )
                if not any(near and near in label for label in labels_to_check):
                    continue
                x1, y1 = node.bounds.center
                x2, y2 = candidate.bounds.center
                distance = hypot(x1 - x2, y1 - y2)
                nearest_distance = (
                    distance if nearest_distance is None else min(nearest_distance, distance)
                )
            if nearest_distance is not None:
                diagonal = max(1.0, hypot(snapshot.width, snapshot.height))
                score += max(0.0, 1.8 * (1.0 - nearest_distance / diagonal))
                reasons.append("near")

        if node.clickable:
            score += 0.75
            reasons.append("clickable")
        if node.focusable and target.role == "input":
            score += 0.6
            reasons.append("focusable")
        if node.bounds and (node.bounds.width < 2 or node.bounds.height < 2):
            score -= 5.0

        return MatchResult(
            node=node,
            score=score,
            reason=", ".join(dict.fromkeys(reasons)),
            exact=exact,
        )
