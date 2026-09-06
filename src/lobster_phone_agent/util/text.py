from __future__ import annotations

import re
import unicodedata

_WHITESPACE = re.compile(r"\s+")
_PUNCT = re.compile(r"[\s\-—_·•,，。.!！?？:：;；'\"“”‘’()（）\[\]{}<>《》/\\]+")


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKC", value).strip().lower()
    normalized = _WHITESPACE.sub(" ", normalized)
    return normalized


def compact_text(value: str | None) -> str:
    return _PUNCT.sub("", normalize_text(value))


def tokenize(value: str | None) -> set[str]:
    normalized = normalize_text(value)
    latin = set(re.findall(r"[a-z0-9_.]+", normalized))
    chinese = set(re.findall(r"[\u4e00-\u9fff]", normalized))
    bigrams = {
        normalized[index : index + 2]
        for index in range(max(0, len(normalized) - 1))
        if not normalized[index : index + 2].isspace()
    }
    return latin | chinese | bigrams


def redact_text(value: str, max_length: int = 160) -> str:
    value = re.sub(r"\b\d{6}\b", "<otp>", value)
    value = re.sub(r"(?i)(password|密码)\s*[:：]?\s*\S+", r"\1=<redacted>", value)
    return value[:max_length]
