"""Tests for data models and name normalization."""

from __future__ import annotations

from custom_components.alexa_shopping_sync.const import PendingOpType
from custom_components.alexa_shopping_sync.models import (
    AlexaShoppingItem,
    ItemMapping,
    ItemSource,
    PendingOperation,
    SyncState,
    normalize_name,
)


class TestNormalizeName:
    """Tests for name normalization."""

    def test_basic_trim(self):
        assert normalize_name("  Milk  ") == "milk"

    def test_multiple_spaces(self):
        assert normalize_name("Whole   Grain   Bread") == "whole grain bread"

    def test_casefold(self):
        assert normalize_name("MILK") == "milk"
        assert normalize_name("Milk") == "milk"

    def test_unicode_nfkc(self):
        # ﬁ (U+FB01) should normalize to "fi"
        assert normalize_name("ﬁsh") == "fish"

    def test_german_umlauts(self):
        # Umlauts should be preserved by casefold (ä stays ä)
        result = normalize_name("Äpfel")
        assert result == "äpfel"

    def test_german_eszett(self):
        # ß casefolds to "ss"
        result = normalize_name("Straße")
        assert result == "strasse"

    def test_empty_string(self):
        assert normalize_name("") == ""

    def test_only_spaces(self):
        assert normalize_name("   ") == ""

    def test_tab_and_newlines(self):
        assert normalize_name("Milk\t\n") == "milk"

    def test_comparison_equal(self):
        assert normalize_name("  Milk ") == normalize_name("milk")
        assert normalize_name("BREAD") == normalize_name("bread")

    def test_comparison_not_equal(self):
        assert normalize_name("Milk") != normalize_name("Bread")


class TestAlexaShoppingItem:
    """Tests for AlexaShoppingItem."""

    def test_from_api_response_standard(self):
        data = {
            "id": "item-123",
            "value": "Milk",
            "completed": False,
            "createdDateTime": 1700000000000,
            "updatedDateTime": 1700000001000,
            "version": 1,
        }
        item = AlexaShoppingItem.from_api_response(data)
        assert item.item_id == "item-123"
        assert item.name == "Milk"
        assert item.complete is False
        assert item.version == 1

    def test_from_api_response_minimal(self):
        """Test with minimal fields."""
        data = {
            "id": "item-456",
            "value": "Bread",
            "completed": True,
        }
        item = AlexaShoppingItem.from_api_response(data)
        assert item.item_id == "item-456"
        assert item.name == "Bread"
        assert item.complete is True

    def test_normalized_name(self):
        item = AlexaShoppingItem(item_id="1", name="  Whole Milk  ", complete=False)
        assert item.normalized_name == "whole milk"


class TestItemMapping:
    """Tests for ItemMapping serialization."""

    def test_round_trip(self):
        mapping = ItemMapping(
            alexa_id="a1",
            ha_id="h1",
            name="Milk",
            last_synced="12345",
            source=ItemSource.ALEXA,
        )
        d = mapping.to_dict()
        restored = ItemMapping.from_dict(d)
        assert restored.alexa_id == "a1"
        assert restored.ha_id == "h1"
        assert restored.source == ItemSource.ALEXA


class TestSyncState:
    """Tests for SyncState persistence."""

    def test_round_trip(self):
        state = SyncState(
            shopping_list_id="list-1",
            last_successful_sync="999",
        )
        state.mappings.append(ItemMapping("a1", "h1", "Milk", "123", ItemSource.ALEXA))
        state.pending_ops.append(
            PendingOperation(PendingOpType.ADD, ItemSource.HA, "Bread", "t1", 100.0)
        )

        d = state.to_dict()
        restored = SyncState.from_dict(d)

        assert restored.shopping_list_id == "list-1"
        assert len(restored.mappings) == 1
        assert restored.mappings[0].name == "Milk"
        assert len(restored.pending_ops) == 1
        assert restored.pending_ops[0].item_name == "Bread"
