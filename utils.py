import re


def phone_normalize(raw: str) -> str | None:
    raw = raw.strip()
    if not re.fullmatch(r"\+?[0-9 ()-]{7,25}", raw):
        return None
    digits = re.sub(r"\D", "", raw)
    if not 7 <= len(digits) <= 15:
        return None
    return ("+" if raw.startswith("+") else "") + digits
