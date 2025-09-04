import pytest
from sprinkler import parse_days


def test_parse_days_valid_numeric():
    assert parse_days("1,5") == [1, 5]


def test_parse_days_invalid_numeric():
    with pytest.raises(ValueError):
        parse_days("8")
