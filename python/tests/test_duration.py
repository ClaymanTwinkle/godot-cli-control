from __future__ import annotations

import pytest

from godot_cli_control._duration import parse_duration


def test_parse_seconds() -> None:
    assert parse_duration("30s") == 30

def test_parse_minutes() -> None:
    assert parse_duration("30m") == 1800

def test_parse_hours() -> None:
    assert parse_duration("2h") == 7200

def test_parse_zero_is_zero() -> None:
    assert parse_duration("0") == 0

def test_parse_bare_int_means_seconds() -> None:
    assert parse_duration("90") == 90

def test_parse_invalid_raises() -> None:
    with pytest.raises(ValueError, match="invalid duration"):
        parse_duration("two minutes")
