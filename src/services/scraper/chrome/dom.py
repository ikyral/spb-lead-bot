"""Pydantic model of a Chrome DevTools Protocol DOM tree node."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field, field_validator


class DOMNode(BaseModel):
    """DOM Node returned by CDP `DOM.getDocument`."""

    id: int = Field(..., alias="nodeId")
    backend_id: int = Field(..., alias="backendNodeId")
    type: int = Field(..., alias="nodeType")
    name: str = Field(..., alias="nodeName")
    local_name: str = Field(..., alias="localName")
    value: str = Field(..., alias="nodeValue")
    children: list[DOMNode] = Field(default_factory=list)
    attributes: dict[str, str] = Field(default_factory=dict)

    @field_validator("attributes", mode="before")
    @classmethod
    def validate_attributes(cls, attributes_list: Any) -> dict[str, str]:
        """CDP encodes node attributes as a flat `[k1, v1, k2, v2, ...]` list."""
        if attributes_list is None:
            return {}
        if isinstance(attributes_list, dict):
            return {str(k): str(v) for k, v in attributes_list.items()}
        if not isinstance(attributes_list, list):
            raise TypeError("DOMNode attributes must be a list or dict")
        if len(attributes_list) % 2 != 0:
            raise ValueError("DOMNode attributes list must have an even length")
        return {
            str(attributes_list[idx]): str(attributes_list[idx + 1])
            for idx in range(0, len(attributes_list), 2)
        }

    def search(self, predicate: Callable[[DOMNode], bool]) -> list[DOMNode]:
        """Search nodes recursively in the DOM subtree matching `predicate`."""
        found: list[DOMNode] = []

        def _visit(node: DOMNode) -> None:
            if predicate(node):
                found.append(node)
            for child in node.children:
                _visit(child)

        _visit(self)
        return found
