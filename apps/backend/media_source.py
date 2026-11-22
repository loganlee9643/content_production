import re


MEDIA_URL_PATTERN = r"^https://([a-z0-9-]+[.])+(cdninstagram[.]com|fbcdn[.]net)(:443)?/[^[:space:]]+$"
_PYTHON_PATTERN = re.compile(MEDIA_URL_PATTERN.replace("[:space:]", r"\s"), re.IGNORECASE)


def allowed_media_url(value):
    return value if isinstance(value, str) and _PYTHON_PATTERN.fullmatch(value) else None
