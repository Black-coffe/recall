"""Secret-shape detector: what is a secret and what is an ordinary argument."""
from __future__ import annotations

import pytest

from app.services.secret_shape import find_secret_args, looks_like_secret


@pytest.mark.parametrize("text", [
    "abcd efgh ijkl mnop",
    "abcdefghijklmnop",
    "my password is abcdefghijklmnop ok",
    "sk-ant-api03-InventedValue123",
])
def test_secret_shapes_detected(text):
    assert looks_like_secret(text)


@pytest.mark.parametrize("text", [
    "abcdefghijklmno",
    "abcdefghijklmnopq",
    "Привіт, як справи сьогодні вранці",
    "ivan.petrenko@example.com",
    "what did we agree about the budget",
    "",
])
def test_ordinary_text_passes(text):
    assert not looks_like_secret(text)


def test_find_secret_args_names_only_and_nested():
    args = {"query": "budget", "address": "abcd efgh ijkl mnop", "tags": ["x", {"k": "sk-ant-abcdef123"}], "n": 5}
    assert find_secret_args(args) == ["address", "tags"]
    assert find_secret_args({"query": "budget"}) == []


@pytest.mark.parametrize("text", [
    "internationalism and abcdefghijklmnop",
    "abcdefghijklmnop",
    "Привіт, як справи сьогодні вранці",
])
def test_lenient_mode_skips_the_bare_16_letter_rule(text):
    assert not looks_like_secret(text, strict=False)


@pytest.mark.parametrize("text", ["abcd efgh ijkl mnop", "see sk-ant-api03-InventedValue123"])
def test_lenient_mode_still_refuses_grouped_and_anthropic(text):
    assert looks_like_secret(text, strict=False)


def test_find_secret_args_lenient():
    args = {"query": "internationalism and abcdefghijklmnop", "tags": ["abcd efgh ijkl mnop"]}
    assert find_secret_args(args, strict=False) == ["tags"]
    assert find_secret_args(args) == ["query", "tags"]
