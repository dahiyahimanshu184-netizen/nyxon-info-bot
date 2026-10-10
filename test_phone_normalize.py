import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("nyxon_utils", Path(__file__).parents[1] / "utils.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def test_phone_normalize_accepts_international_number():
    assert mod.phone_normalize("+1 (415) 555-2671") == "+14155552671"


def test_phone_normalize_accepts_digits():
    assert mod.phone_normalize("447700900123") == "447700900123"


def test_phone_normalize_rejects_letters():
    assert mod.phone_normalize("call-me-please") is None


def test_phone_normalize_rejects_too_short():
    assert mod.phone_normalize("12345") is None


def test_phone_normalize_rejects_too_many_digits():
    assert mod.phone_normalize("123456789012345678") is None
