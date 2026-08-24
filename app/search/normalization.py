import re
import unicodedata


_WHITESPACE = re.compile(r"\s+")
_TOKEN = re.compile(r"[a-z0-9]+(?:[.&+-][a-z0-9]+)*|[가-힣]+")


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return _WHITESPACE.sub(" ", normalized).strip()


def tokenize(value: str) -> list[str]:
    return _TOKEN.findall(normalize_text(value))
