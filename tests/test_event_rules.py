import pytest

from server.services.event_rules import (
    CRITERIA,
    DISQUALIFY_RULES,
    InvalidInput,
    hash_token,
    issue_participant_token,
    normalize_name,
    normalize_phone,
)
from workers.optimizer.scoring import MAX_TOTAL


def test_criteria_add_up_to_the_max_score():
    assert sum(c["max_score"] for c in CRITERIA) == MAX_TOTAL == 100


@pytest.mark.parametrize("raw", ["010-1234-5678", "01012345678", " 010 1234 5678 ", "0212345678"])
def test_phone_is_normalized_to_digits(raw):
    assert normalize_phone(raw).isdigit()


@pytest.mark.parametrize("raw", ["", "1234", "010-1234-56789", "11012345678", "abc"])
def test_invalid_phone_is_rejected(raw):
    with pytest.raises(InvalidInput):
        normalize_phone(raw)


def test_name_is_trimmed_and_limited():
    assert normalize_name("  예원  ") == "예원"
    assert normalize_name("가  나") == "가 나"
    with pytest.raises(InvalidInput):
        normalize_name("   ")
    with pytest.raises(InvalidInput):
        normalize_name("가" * 13)


def test_token_is_stored_only_as_hash():
    token, token_hash = issue_participant_token()
    assert token.startswith("pt_")
    assert token_hash == hash_token(token)
    assert token not in token_hash
    assert issue_participant_token()[0] != token


def test_no_automatic_zero_or_disqualification_rules():
    assert DISQUALIFY_RULES == []
