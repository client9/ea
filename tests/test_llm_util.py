"""
Tests for ea.llm_util.strip_json_fences().
"""

from ea.llm_util import strip_json_fences


def test_plain_json_passthrough():
    raw = '{"a": 1}'
    assert strip_json_fences(raw) == raw


def test_json_fence_stripped():
    raw = '```json\n{"a": 1}\n```'
    assert strip_json_fences(raw) == '{"a": 1}'


def test_bare_fence_stripped():
    raw = '```\n{"a": 1}\n```'
    assert strip_json_fences(raw) == '{"a": 1}'


def test_whitespace_inside_fence_stripped():
    raw = '```json\n  {"a": 1}  \n```'
    assert strip_json_fences(raw) == '{"a": 1}'


def test_empty_string_passthrough():
    assert strip_json_fences("") == ""


def test_no_fence_no_change():
    raw = "just some text"
    assert strip_json_fences(raw) == raw
