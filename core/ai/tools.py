"""
core/ai/tools.py — one tool description, three dialects.

THE PROBLEM
    Alexio's 24 tool declarations are written in Gemini's dialect: types spelled
    in capitals (`"type": "OBJECT"`, `"type": "STRING"`) inside a `parameters`
    object. Anthropic wants standard JSON Schema in lowercase under
    `input_schema`; OpenAI wants the same schema nested under
    `function.parameters`.

    A gateway that does not translate schemas is not a gateway — it is an
    adapter for one vendor with extra steps. Routing a tool-calling request to a
    second provider without this produces a 400 at best and, at worst, a model
    that silently sees no tools and answers from memory instead of acting.

THE SHAPE
    ToolSpec holds the neutral form: a name, a description, and parameters as
    ordinary JSON Schema. Each dialect is a pure function of that. Two extra
    fields exist for the router rather than for the model — `irreversible` and
    `needs_confirmation` — because whether a tool can be taken back is a
    property of the tool, and today that knowledge is scattered between
    core/confirm.py and the prose in core/prompt.txt.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

# Gemini writes JSON Schema type names in capitals. Everyone else does not.
_GEMINI_TO_JSON = {
    "OBJECT": "object", "STRING": "string", "NUMBER": "number",
    "INTEGER": "integer", "BOOLEAN": "boolean", "ARRAY": "array", "NULL": "null",
}
_JSON_TO_GEMINI = {v: k for k, v in _GEMINI_TO_JSON.items()}


def _convert_types(node: Any, table: dict[str, str]) -> Any:
    """Walk a schema and rewrite every `type` through `table`.

    Recursive because `properties`, `items` and the `anyOf`-style lists all nest
    schemas, and a converter that only touched the top level would produce a
    document that looks right and fails on the first array of objects.
    """
    if isinstance(node, list):
        return [_convert_types(x, table) for x in node]
    if not isinstance(node, dict):
        return node

    out: dict = {}
    for key, value in node.items():
        if key == "type" and isinstance(value, str):
            out[key] = table.get(value, table.get(value.upper(), value))
        else:
            out[key] = _convert_types(value, table)
    return out


@dataclass
class ToolSpec:
    """One capability, described once."""

    name:        str
    description: str
    parameters:  dict = field(default_factory=lambda: {"type": "object", "properties": {}})

    # For the router and the safety gates, never sent to the model.
    irreversible:       bool = False
    needs_confirmation: bool = False

    # ── construction ─────────────────────────────────────────────────────

    @classmethod
    def from_gemini(cls, decl: dict, **flags) -> "ToolSpec":
        """Adopt one of the existing TOOL_DECLARATIONS dicts unchanged."""
        params = decl.get("parameters") or {"type": "OBJECT", "properties": {}}
        return cls(
            name=decl["name"],
            description=decl.get("description", ""),
            parameters=_convert_types(params, _GEMINI_TO_JSON),
            **flags,
        )

    # ── dialects ─────────────────────────────────────────────────────────

    def to_gemini(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": _convert_types(self.parameters, _JSON_TO_GEMINI),
        }

    # deepcopy on both: `to_gemini` builds a new document as a side effect of
    # converting the type names, and these two used to hand out `self.parameters`
    # itself. A provider adapter that normalises the schema it was given —
    # adding a "required" key, lowercasing a type — then edited the ToolSpec
    # every other adapter reads next. The dialects are documented as pure
    # functions of the neutral form; a shared dict makes them anything but.
    def to_anthropic(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": copy.deepcopy(self.parameters),
        }

    def to_openai(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": copy.deepcopy(self.parameters),
            },
        }

    def render(self, dialect: str) -> dict:
        try:
            return {
                "gemini":    self.to_gemini,
                "anthropic": self.to_anthropic,
                "openai":    self.to_openai,
            }[dialect]()
        except KeyError:
            raise ValueError(
                f"Unknown tool dialect {dialect!r}. "
                f"Known: gemini, anthropic, openai."
            ) from None


def render_all(specs: list[ToolSpec], dialect: str) -> list[dict]:
    return [s.render(dialect) for s in specs]


def adopt_gemini_declarations(declarations: list[dict],
                              irreversible: set[str] | None = None) -> list[ToolSpec]:
    """Convert a whole TOOL_DECLARATIONS list.

    `irreversible` names the tools that cannot be undone. It is passed in rather
    than inferred: "sounds alarming" is not the criterion — reversibility is,
    and only the caller knows which of its tools registered an undo.
    """
    irreversible = irreversible or set()
    return [
        ToolSpec.from_gemini(
            d,
            irreversible=d["name"] in irreversible,
            needs_confirmation=d["name"] in irreversible,
        )
        for d in declarations
    ]
