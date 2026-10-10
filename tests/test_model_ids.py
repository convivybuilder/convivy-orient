"""model_ids — the alias → model id table `policy_api.py`'s write boundary and
`policy_build.py`'s lookup/datalist both use (issue_01M2HVGKSNFMHZF0FDN8FBT9N6),
read from `agentctl models` rather than kept by hand."""
from __future__ import annotations

import pytest

import model_catalog
import model_ids

_CATALOG = [
    model_catalog.Model(model="claude-opus-5-5", alias="opus", provider="anthropic"),
    model_catalog.Model(model="claude-sonnet-5", alias="sonnet", provider="anthropic"),
    model_catalog.Model(model="deepseek/deepseek-v4-flash", alias="deepseek", provider="gateway"),
    model_catalog.Model(model="claude-opus-4-8", alias=None, provider="anthropic"),
]


@pytest.fixture
def table(monkeypatch):
    monkeypatch.setattr(model_catalog, "load", lambda: list(_CATALOG))
    return model_ids.aliases()


def test_an_alias_resolves_to_what_agentctl_says_today(table):
    """`opus` is the case the hand-kept copy got wrong: it still said
    `claude-opus-5` after agentctl moved to `claude-opus-5-5`."""
    assert model_ids.resolve("opus", table) == "claude-opus-5-5"
    assert model_ids.resolve("sonnet", table) == "claude-sonnet-5"
    assert model_ids.resolve("deepseek", table) == "deepseek/deepseek-v4-flash"


def test_an_entry_with_no_alias_adds_no_key(table):
    assert None not in table
    assert set(table) == {"opus", "sonnet", "deepseek"}


def test_an_already_resolved_id_passes_through_unchanged(table):
    """Matches agentctl's own `model_id()` fallback: an operator who already
    typed the id (the write form's placeholder example) must write the same
    key as one who typed the alias — never a third, doubly-resolved value."""
    assert model_ids.resolve("claude-sonnet-5", table) == "claude-sonnet-5"


def test_an_unrecognised_value_passes_through_unchanged(table):
    assert model_ids.resolve("some-future-model", table) == "some-future-model"


@pytest.mark.parametrize("unavailable", [None, []])
def test_an_unavailable_list_raises_rather_than_guessing(monkeypatch, unavailable):
    monkeypatch.setattr(model_catalog, "load", lambda: unavailable)
    with pytest.raises(model_ids.ModelListUnavailable):
        model_ids.aliases()
