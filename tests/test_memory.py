"""
memory_manager: the prompt budget, the overflow index, and recall.

The interesting property is the one the module's own docstring calls out: a
model cannot decide to look something up if it does not know the thing exists.
So the prompt must carry an index of the keys it had no room for — and that
index must interleave categories, or a person with forty preferences never gets
their sister's name listed.
"""

from __future__ import annotations

from memory import memory_manager as mm


def _entry(value: str, updated: str = "2026-01-01") -> dict:
    return {"value": value, "updated": updated}


def test_identity_is_always_in_the_prompt(temp_memory):
    mm.save_memory({
        "identity": {"name": _entry("Teddy"), "city": _entry("Lyon")},
        "preferences": {}, "projects": {}, "relationships": {}, "wishes": {}, "notes": {},
    })
    block = mm.format_memory_for_prompt(mm.load_memory())
    assert "Teddy" in block
    assert "Lyon" in block


def test_language_is_labelled_as_an_observation_not_an_instruction(temp_memory):
    """A bare 'Language: English' line read like a standing order and was one of
    the reasons a French question came back in English."""
    mm.save_memory({"identity": {"language": _entry("French")}})
    block = mm.format_memory_for_prompt(mm.load_memory())
    assert "Has spoken to you in: French" in block
    assert "CURRENT message" in block


def test_what_does_not_fit_is_indexed_rather_than_dropped(temp_memory):
    memory = {
        "identity": {"name": _entry("Teddy")},
        "preferences": {f"pref_{i}": _entry(f"value number {i}", "2026-06-01")
                        for i in range(40)},
        "projects": {}, "relationships": {}, "wishes": {}, "notes": {},
    }
    mm.save_memory(memory)
    block = mm.format_memory_for_prompt(mm.load_memory())

    assert "ALSO REMEMBERED" in block
    assert "recall_memory" in block, "the index must tell the model how to read it"
    # Values are omitted from the index; only the keys are listed.
    assert "Pref 39" in block or "pref 39" in block.lower()


def test_the_per_category_cap_keeps_an_old_relationship_in_the_prompt(temp_memory):
    """Pure recency buries the categories that matter most in conversation:
    they are also the ones that change least often. Forty preferences updated
    this year must not push out a sister recorded in 2024."""
    memory = {
        "identity": {"name": _entry("Teddy")},
        "preferences": {f"pref_{i}": _entry(f"value number {i}", "2026-06-01")
                        for i in range(40)},
        "relationships": {"ayse_sister": _entry("older sister", "2024-01-01")},
        "projects": {}, "wishes": {}, "notes": {},
    }
    mm.save_memory(memory)
    block = mm.format_memory_for_prompt(mm.load_memory())

    assert "older sister" in block, "the relationship lost to recency"
    shown_prefs = block.lower().count("value number")
    assert shown_prefs <= mm.PROMPT_MAX_PER_CATEGORY


def test_prompt_core_stays_within_budget(temp_memory):
    memory = {
        "identity": {"name": _entry("Teddy")},
        "notes": {f"note_{i}": _entry("x" * 200, "2026-06-01") for i in range(50)},
        "preferences": {}, "projects": {}, "relationships": {}, "wishes": {},
    }
    mm.save_memory(memory)
    block = mm.format_memory_for_prompt(mm.load_memory())
    # Core + index + headers; the budget is on the core, so allow the index too.
    assert len(block) < mm.PROMPT_CORE_CHARS + mm.PROMPT_INDEX_CHARS + 400


def test_recall_finds_by_key_and_by_value(temp_memory):
    mm.save_memory({
        "relationships": {"ayse_sister": _entry("older sister, lives in Paris")},
        "preferences": {"favorite_food": _entry("pizza")},
        "identity": {}, "projects": {}, "wishes": {}, "notes": {},
    })
    assert "older sister" in mm.search_memory("ayse")
    assert "pizza" in mm.search_memory("food")
    assert "Paris" in mm.search_memory("paris")


def test_recall_with_empty_query_lists_everything(temp_memory):
    mm.save_memory({"identity": {"name": _entry("Teddy")},
                    "preferences": {"drink": _entry("coffee")},
                    "projects": {}, "relationships": {}, "wishes": {}, "notes": {}})
    out = mm.search_memory("")
    assert "Teddy" in out and "coffee" in out


def test_recall_says_so_when_nothing_matches(temp_memory):
    mm.save_memory({"identity": {"name": _entry("Teddy")}})
    assert "Nothing stored" in mm.search_memory("submarine")


def test_values_are_truncated_not_rejected(temp_memory):
    mm.update_memory({"notes": {"long": {"value": "y" * 900}}})
    stored = mm.load_memory()["notes"]["long"]["value"]
    assert len(stored) <= mm.MAX_VALUE_LENGTH + 1     # +1 for the ellipsis
    assert stored.endswith("…")


def test_session_summary_is_consumed_on_read(temp_memory):
    """pop_last_session removes the entry so the morning briefing can never
    mention the same conversation twice."""
    mm.save_memory({"identity": {}, "preferences": {}, "projects": {},
                    "relationships": {}, "wishes": {}, "notes": {}})
    mm.save_session_summary("We set up the new router.", "French")

    first = mm.pop_last_session()
    assert first is not None and "router" in first["summary"]
    assert mm.pop_last_session() is None


def test_a_corrupt_store_does_not_take_down_the_assistant(temp_memory):
    temp_memory.write_text("{ this is not json", encoding="utf-8")
    assert mm.load_memory() == mm._empty_memory()
