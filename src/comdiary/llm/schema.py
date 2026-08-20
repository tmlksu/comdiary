"""pydantic JSON Schema → Gemini ``responseSchema``.

The Gemini API constrains decoding to a schema, which is exactly what ADR 0001
wants: the model cannot emit prose around the JSON in the first place. But the
schema dialect it accepts is a subset of OpenAPI 3.0, not JSON Schema, and the
two disagree in ways that matter here:

* ``$ref`` / ``$defs`` do not exist — pydantic emits them for every nested model
  (``Segment``, ``Signal``, ``Decision``, ...), so the schema has to be inlined.
* ``X | None`` becomes ``anyOf: [{...}, {"type": "null"}]`` in JSON Schema, but
  ``nullable: true`` in OpenAPI. Half our optional fields look like this.
* Type names are proto enum values, i.e. upper case.

Anything this module cannot express faithfully raises `SchemaUnsupported`, and
the backend falls back to an unconstrained call plus the existing retry loop.
Silently dropping a constraint would be worse: the model would be free to emit
a shape pydantic then rejects, and the failure would look like a model problem.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

#: OpenAPI type names are proto enum values. Lower case happens to work in some
#: code paths and not others; upper case is what the proto actually defines.
_TYPES = {
    "string": "STRING",
    "integer": "INTEGER",
    "number": "NUMBER",
    "boolean": "BOOLEAN",
    "array": "ARRAY",
    "object": "OBJECT",
}

#: Formats the API recognises. An unknown ``format`` is rejected outright rather
#: than ignored, so anything else (``date``, ``uuid``, ``email``) has to go.
_FORMATS = {
    "STRING": {"date-time"},
    "INTEGER": {"int32", "int64"},
    "NUMBER": {"float", "double"},
}

#: Constraint keywords that survive the translation unchanged.
_KEEP = ("minimum", "maximum", "minItems", "maxItems", "minLength", "maxLength", "pattern")

#: Nested models are shallow here; anything deeper is a recursive schema, which
#: the subset cannot express at all.
_MAX_DEPTH = 12


class SchemaUnsupported(ValueError):
    """The schema cannot be expressed in Gemini's OpenAPI subset."""


def _deref(node: dict, defs: dict, seen: tuple[str, ...]) -> tuple[dict, tuple[str, ...]]:
    """Follow ``$ref`` into ``$defs``, refusing to loop."""
    ref = node.get("$ref")
    if not ref:
        return node, seen
    if not ref.startswith("#/$defs/"):
        raise SchemaUnsupported(f"外部参照は展開できません: {ref}")
    name = ref[len("#/$defs/") :]
    if name in seen:
        raise SchemaUnsupported(f"再帰的なスキーマは展開できません: {name}")
    if name not in defs:
        raise SchemaUnsupported(f"$defs に {name} がありません")
    target = dict(defs[name])
    # Sibling keys next to a $ref (pydantic puts `description` there) win, since
    # they are the field's own annotation rather than the model's.
    for key, value in node.items():
        if key != "$ref":
            target[key] = value
    return target, (*seen, name)


def _strip_null(branches: list[dict]) -> tuple[list[dict], bool]:
    kept = [b for b in branches if b.get("type") != "null"]
    return kept, len(kept) != len(branches)


def _convert(node: Any, defs: dict, depth: int = 0, seen: tuple[str, ...] = ()) -> dict:
    if depth > _MAX_DEPTH:
        raise SchemaUnsupported("スキーマの入れ子が深すぎます")
    if not isinstance(node, dict):
        raise SchemaUnsupported(f"スキーマの節が dict ではありません: {node!r}")

    node, seen = _deref(node, defs, seen)

    # pydantic wraps a $ref in allOf when the field also carries a default.
    if len(node.get("allOf", ())) == 1:
        merged = dict(node)
        inner = merged.pop("allOf")[0]
        return _convert({**inner, **merged}, defs, depth, seen)

    out: dict[str, Any] = {}
    if description := node.get("description"):
        out["description"] = description

    if "anyOf" in node or "oneOf" in node:
        branches, nullable = _strip_null(list(node.get("anyOf") or node.get("oneOf")))
        if not branches:
            raise SchemaUnsupported("null しか許さないフィールドは表現できません")
        if len(branches) == 1:
            # `X | None` — by far the common case, and it collapses cleanly.
            merged = _convert(branches[0], defs, depth + 1, seen)
            merged.update(out)
            if nullable:
                merged["nullable"] = True
            return merged
        out["anyOf"] = [_convert(b, defs, depth + 1, seen) for b in branches]
        if nullable:
            out["nullable"] = True
        return out

    # Literal["a"] emits const rather than a one-element enum.
    if "const" in node:
        out["type"] = "STRING"
        out["enum"] = [str(node["const"])]
        return out

    raw_type = node.get("type")
    if raw_type is None:
        raise SchemaUnsupported(f"type の無いスキーマは扱えません: {sorted(node)}")
    if isinstance(raw_type, list):
        kept = [t for t in raw_type if t != "null"]
        if len(kept) != 1:
            raise SchemaUnsupported(f"複数型のフィールドは表現できません: {raw_type}")
        out["nullable"] = len(kept) != len(raw_type)
        raw_type = kept[0]
    if raw_type not in _TYPES:
        raise SchemaUnsupported(f"未対応の型: {raw_type}")
    kind = _TYPES[raw_type]
    out["type"] = kind

    if (fmt := node.get("format")) and fmt in _FORMATS.get(kind, ()):
        out["format"] = fmt

    if enum := node.get("enum"):
        out["enum"] = [str(v) for v in enum]

    for key in _KEEP:
        if key in node:
            out[key] = node[key]

    if kind == "ARRAY":
        items = node.get("items")
        if items is None:
            raise SchemaUnsupported("items の無い配列は表現できません")
        out["items"] = _convert(items, defs, depth + 1, seen)

    if kind == "OBJECT":
        properties = node.get("properties")
        if not properties:
            # dict[str, X] has no fixed properties; the subset has no way to say
            # "arbitrary keys", so this must not be silently turned into {}.
            raise SchemaUnsupported("プロパティを列挙できないオブジェクトは表現できません")
        out["properties"] = {
            name: _convert(sub, defs, depth + 1, seen) for name, sub in properties.items()
        }
        # Documented to improve output quality, and it costs nothing: without it
        # the model picks its own field order per call.
        out["propertyOrdering"] = list(properties)
        if required := [r for r in node.get("required", []) if r in properties]:
            out["required"] = required

    return out


def to_gemini_schema(model: type[BaseModel]) -> dict:
    """Translate a pydantic model's JSON Schema, or raise `SchemaUnsupported`."""
    source = model.model_json_schema()
    return _convert(source, source.get("$defs", {}))
