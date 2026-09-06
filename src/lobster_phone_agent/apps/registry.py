from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, StrictStr, field_validator

from lobster_phone_agent.contracts import ContractModel, PACKAGE_PATTERN
from rapidfuzz.fuzz import ratio

from lobster_phone_agent.util.text import compact_text


class AppRecord(ContractModel):
    name: StrictStr = Field(min_length=1, max_length=128)
    package: StrictStr | None = Field(default=None, max_length=255, pattern=PACKAGE_PATTERN)
    aliases: list[StrictStr] = Field(default_factory=list, max_length=64)
    category: str = "other"
    launch_activity: str | None = None
    deep_links: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("aliases", mode="before")
    @classmethod
    def normalize_aliases(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError("aliases must be an array of strings")
        return [item.strip() for item in value if item.strip()]

    def all_names(self) -> list[str]:
        return list(dict.fromkeys([self.name, *self.aliases, self.package or ""]))


class AppRegistry:
    def __init__(self, records: list[AppRecord] | None = None) -> None:
        self.records = records or []
        self._by_package = {
            record.package: record for record in self.records if record.package
        }

    @classmethod
    def from_yaml(cls, path: Path) -> AppRegistry:
        if not path.exists():
            return cls([])
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        if isinstance(raw, dict):
            raw = raw.get("apps", [])
        return cls([AppRecord.model_validate(item) for item in raw])

    def with_task_catalog(self, catalog: list[dict[str, Any]]) -> AppRegistry:
        merged = list(self.records)
        seen = {record.package for record in merged if record.package}
        for item in catalog:
            try:
                record = AppRecord.model_validate(item)
            except Exception as exc:
                raise ValueError("invalid task app catalog record") from exc
            if record.package and record.package in seen:
                continue
            merged.append(record)
            if record.package:
                seen.add(record.package)
        return AppRegistry(merged)

    def resolve(
        self,
        query: str,
        *,
        installed_apps: list[dict[str, object]] | None = None,
        explicit_package: str | None = None,
    ) -> AppRecord | None:
        if explicit_package:
            return self._by_package.get(explicit_package) or AppRecord(
                name=query or explicit_package,
                package=explicit_package,
                aliases=[query] if query else [],
            )

        normalized = compact_text(query)
        if not normalized:
            return None

        for record in self.records:
            if record.package == query:
                return record
            if any(compact_text(name) == normalized for name in record.all_names()):
                return record

        installed = self._normalize_installed(installed_apps or [])
        for package, label in installed:
            if compact_text(package) == normalized or compact_text(label) == normalized:
                return self._by_package.get(package) or AppRecord(
                    name=label or query,
                    package=package,
                    aliases=[query],
                )

        candidates: list[tuple[float, AppRecord]] = []
        for record in self.records:
            best = max(
                ratio(normalized, compact_text(name))
                for name in record.all_names()
                if compact_text(name)
            )
            if best >= 72:
                candidates.append((float(best), record))

        for package, label in installed:
            candidate_names = [package, label]
            best = max(
                ratio(normalized, compact_text(name))
                for name in candidate_names
                if compact_text(name)
            )
            if best >= 72:
                record = self._by_package.get(package) or AppRecord(
                    name=label or package,
                    package=package,
                )
                candidates.append((float(best), record))

        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0], reverse=True)
        if len(candidates) > 1:
            top_score, top_record = candidates[0]
            second_score, second_record = candidates[1]
            if (
                top_record.package != second_record.package
                and top_score - second_score < 6.0
            ):
                return None
        return candidates[0][1]

    @staticmethod
    def _normalize_installed(
        items: list[dict[str, object]],
    ) -> list[tuple[str, str]]:
        result: list[tuple[str, str]] = []
        for item in items:
            package = str(
                item.get("package")
                or item.get("packageName")
                or item.get("id")
                or ""
            )
            label = str(item.get("name") or item.get("label") or "")
            if package:
                result.append((package, label))
        return result
