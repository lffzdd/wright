"""Rough token estimates: UTF-8 bytes over four, rounded up, plus message framing."""

from wright.common.constants import MESSAGE_OVERHEAD_TOKENS
from wright.utils.token_counter import estimate_message_tokens, estimate_tokens


def test_english_stays_about_four_characters_a_token():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcdefgh") == 2
    assert estimate_tokens("abcdefghi") == 3


def test_cjk_is_not_counted_at_a_quarter_of_its_size():
    # Three UTF-8 bytes a character: 6 characters are 18 bytes, 5 tokens.
    assert estimate_tokens("现在有什么呢") == 5
    assert estimate_tokens("行") == 1


def test_short_text_and_messages_are_never_free():
    assert estimate_tokens("AI") == 1
    assert estimate_message_tokens({"role": "user", "content": "AI"}) == 1 + MESSAGE_OVERHEAD_TOKENS
    assert estimate_message_tokens({"role": "assistant", "content": ""}) == MESSAGE_OVERHEAD_TOKENS
