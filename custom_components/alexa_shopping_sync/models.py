"""Data models for Alexa Shopping List Sync."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from .const import PendingOpType


def normalize_name(name: str) -> str:
    """Normalize an item name for comparison.

    - trim whitespace
    - collapse multiple spaces
    - Unicode NFKC normalization
    - casefold for case-insensitive comparison
    """
    text = unicodedata.normalize("NFKC", name)
    text = " ".join(text.split())
    return text.casefold()


class ItemSource(StrEnum):
    """Source of an item."""

    ALEXA = "alexa"
    HA = "ha"


@dataclass
class AlexaShoppingItem:
    """An item from the Alexa shopping list."""

    item_id: str
    name: str
    complete: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None
    version: int | None = None

    @property
    def normalized_name(self) -> str:
        """Return normalized name for matching."""
        return normalize_name(self.name)

    @classmethod
    def from_api_response(cls, data: dict[str, Any]) -> AlexaShoppingItem:
        """Create from Amazon API response.

        Verified field names from live API (2026-03):
          id: UUID string (e.g. "28968840-d612-4baa-b6ae-0228dd9960ac")
          value: item text (e.g. "milch")
          completed: bool
          version: int
          createdDateTime: epoch ms
          updatedDateTime: epoch ms
          listId: base64-encoded list ID
          categoryValue: auto-categorized string (e.g. "Dairy")
          customerId: Amazon customer ID
          itemType: "KEYWORD"
        """
        created = None
        updated = None
        if "createdDateTime" in data:
            try:
                created = datetime.fromtimestamp(data["createdDateTime"] / 1000)
            except (ValueError, TypeError, OSError):
                pass
        if "updatedDateTime" in data:
            try:
                updated = datetime.fromtimestamp(data["updatedDateTime"] / 1000)
            except (ValueError, TypeError, OSError):
                pass

        return cls(
            item_id=data.get("id", ""),
            name=data.get("value", ""),
            complete=data.get("completed", False),
            created_at=created,
            updated_at=updated,
            version=data.get("version"),
        )


@dataclass
class HAShoppingItem:
    """An item from the HA shopping list."""

    item_id: str
    name: str
    complete: bool

    @property
    def normalized_name(self) -> str:
        """Return normalized name for matching."""
        return normalize_name(self.name)


@dataclass
class ItemMapping:
    """Mapping between Alexa and HA item IDs."""

    alexa_id: str
    ha_id: str
    name: str
    last_synced: str
    source: ItemSource

    def to_dict(self) -> dict[str, str]:
        """Serialize to dict."""
        return {
            "alexa_id": self.alexa_id,
            "ha_id": self.ha_id,
            "name": self.name,
            "last_synced": self.last_synced,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ItemMapping:
        """Deserialize from dict."""
        return cls(
            alexa_id=data["alexa_id"],
            ha_id=data["ha_id"],
            name=data["name"],
            last_synced=data["last_synced"],
            source=ItemSource(data.get("source", ItemSource.ALEXA)),
        )


@dataclass
class PendingOperation:
    """A pending sync operation for echo suppression."""

    op_type: PendingOpType
    source: ItemSource
    item_name: str
    target_id: str | None = None
    created_at: float = 0.0
    confirmed: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict."""
        return {
            "op_type": self.op_type,
            "source": self.source,
            "item_name": self.item_name,
            "target_id": self.target_id,
            "created_at": self.created_at,
            "confirmed": self.confirmed,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PendingOperation:
        """Deserialize from dict."""
        return cls(
            op_type=PendingOpType(data["op_type"]),
            source=ItemSource(data["source"]),
            item_name=data["item_name"],
            target_id=data.get("target_id"),
            created_at=data.get("created_at", 0.0),
            confirmed=data.get("confirmed", False),
        )


@dataclass
class SyncState:
    """Persistent sync state."""

    mappings: list[ItemMapping] = field(default_factory=list)
    pending_ops: list[PendingOperation] = field(default_factory=list)
    shopping_list_id: str = ""
    last_alexa_snapshot_hash: str = ""
    last_ha_snapshot_hash: str = ""
    last_successful_sync: str = ""
    last_error: str = ""
    version: int = 1

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict."""
        return {
            "mappings": [m.to_dict() for m in self.mappings],
            "pending_ops": [p.to_dict() for p in self.pending_ops],
            "shopping_list_id": self.shopping_list_id,
            "last_alexa_snapshot_hash": self.last_alexa_snapshot_hash,
            "last_ha_snapshot_hash": self.last_ha_snapshot_hash,
            "last_successful_sync": self.last_successful_sync,
            "last_error": self.last_error,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SyncState:
        """Deserialize from dict."""
        return cls(
            mappings=[ItemMapping.from_dict(m) for m in data.get("mappings", [])],
            pending_ops=[
                PendingOperation.from_dict(p) for p in data.get("pending_ops", [])
            ],
            shopping_list_id=data.get("shopping_list_id", ""),
            last_alexa_snapshot_hash=data.get("last_alexa_snapshot_hash", ""),
            last_ha_snapshot_hash=data.get("last_ha_snapshot_hash", ""),
            last_successful_sync=data.get("last_successful_sync", ""),
            last_error=data.get("last_error", ""),
            version=data.get("version", 1),
        )
