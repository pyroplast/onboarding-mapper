"""Target schemas: the shape the customer's data has to end up in."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from . import lexicon
from .normalize import norm_header, norm_text

FIELD_TYPES = {
    "id",
    "string",
    "name",
    "email",
    "phone",
    "date",
    "enum",
    "country",
    "currency",
    "decimal",
    "ratio",
    "ref",
}
RULE_TYPES = {"date_order", "min_gap_years", "required_when", "required_with"}


class SchemaError(ValueError):
    pass


@dataclass(frozen=True)
class Field:
    name: str
    type: str
    required: bool = False
    unique: bool = False
    concept: str = ""
    description: str = ""
    values: tuple[str, ...] = ()
    ref: str = ""
    aliases: tuple[tuple[str, float], ...] = ()  # normalised header phrases with weights
    enum_lookup: tuple[tuple[str, str], ...] = ()  # normalised source value -> canonical value
    options: tuple[tuple[str, Any], ...] = ()

    def opt(self, key: str, default: Any = None) -> Any:
        return dict(self.options).get(key, default)

    @property
    def enum_map(self) -> dict[str, str]:
        return dict(self.enum_lookup)


@dataclass(frozen=True)
class Schema:
    name: str
    version: str
    description: str
    fields: tuple[Field, ...]
    rules: tuple[dict, ...] = field(default_factory=tuple)
    sha256: str = ""

    def get(self, name: str) -> Field:
        for f in self.fields:
            if f.name == name:
                return f
        raise KeyError(name)

    def has(self, name: str) -> bool:
        return any(f.name == name for f in self.fields)

    def by_concept(self, concept: str) -> Field | None:
        return next((f for f in self.fields if f.concept == concept), None)

    @property
    def field_names(self) -> list[str]:
        return [f.name for f in self.fields]


_OPTION_KEYS = (
    "min_age_years",
    "max_age_years",
    "max_future_days",
    "min_exclusive",
    "min",
    "max",
    "places",
    "max_length",
)


def _build_field(raw: dict) -> Field:
    name = raw.get("name")
    if not name or not isinstance(name, str):
        raise SchemaError("every field needs a string 'name'")
    ftype = raw.get("type", "string")
    if ftype not in FIELD_TYPES:
        raise SchemaError(f"field '{name}': unknown type '{ftype}' (known: {', '.join(sorted(FIELD_TYPES))})")
    concept = raw.get("concept", "")
    if concept and concept not in lexicon.ALIASES:
        raise SchemaError(f"field '{name}': unknown concept '{concept}'")

    aliases: dict[str, float] = {norm_header(name): 1.0}
    for phrase, weight in lexicon.ALIASES.get(concept, []):
        aliases[phrase] = max(aliases.get(phrase, 0.0), weight)
    for item in raw.get("aliases", []):
        phrase, weight = (item, 1.0) if isinstance(item, str) else (item[0], float(item[1]))
        aliases[norm_header(phrase)] = max(aliases.get(norm_header(phrase), 0.0), weight)

    values = tuple(raw.get("values", []))
    enum_lookup: dict[str, str] = {}
    if ftype == "enum":
        if not values:
            raise SchemaError(f"enum field '{name}' needs 'values'")
        for key, canon in lexicon.ENUM_SYNONYMS.get(concept, {}).items():
            if canon in values:
                enum_lookup[key] = canon
        for canon, synonyms in raw.get("synonyms", {}).items():
            if canon not in values:
                raise SchemaError(f"field '{name}': synonyms given for unknown value '{canon}'")
            for s in synonyms:
                enum_lookup[norm_text(s)] = canon
        for canon in values:
            enum_lookup[norm_text(canon)] = canon
    if ftype == "ref" and not raw.get("ref"):
        raise SchemaError(f"ref field '{name}' needs 'ref' (the field it points at)")

    return Field(
        name=name,
        type=ftype,
        required=bool(raw.get("required", False)),
        unique=bool(raw.get("unique", False)),
        concept=concept,
        description=raw.get("description", ""),
        values=values,
        ref=raw.get("ref", ""),
        aliases=tuple(sorted(aliases.items())),
        enum_lookup=tuple(sorted(enum_lookup.items())),
        options=tuple((k, raw[k]) for k in _OPTION_KEYS if k in raw),
    )


def from_dict(raw: dict, sha256: str = "") -> Schema:
    if not isinstance(raw.get("fields"), list) or not raw["fields"]:
        raise SchemaError("schema needs a non-empty 'fields' list")
    fields = tuple(_build_field(f) for f in raw["fields"])
    names = [f.name for f in fields]
    if len(set(names)) != len(names):
        raise SchemaError("field names must be unique")
    for f in fields:
        if f.type == "ref" and f.ref not in names:
            raise SchemaError(f"field '{f.name}' refers to unknown field '{f.ref}'")
    rules = tuple(raw.get("rules", []))
    for rule in rules:
        if rule.get("type") not in RULE_TYPES:
            raise SchemaError(f"rule '{rule.get('id')}': unknown type '{rule.get('type')}'")
        for key in ("earlier", "later", "field", "with"):
            if key in rule and rule[key] not in names:
                raise SchemaError(f"rule '{rule.get('id')}' names unknown field '{rule[key]}'")
    return Schema(
        name=raw.get("name", "custom"),
        version=str(raw.get("version", "1")),
        description=raw.get("description", ""),
        fields=fields,
        rules=rules,
        sha256=sha256,
    )


def builtin_names() -> list[str]:
    return sorted(
        p.name[:-5]
        for p in resources.files("onboarding_mapper").joinpath("schemas").iterdir()
        if p.name.endswith(".json")
    )


def load(name_or_path: str) -> Schema:
    """Load a built-in schema by name ("employee") or a custom one from a JSON file."""
    path = Path(name_or_path)
    if path.suffix == ".json" or path.exists():
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise SchemaError(f"cannot read schema file: {exc}") from exc
    else:
        ref = resources.files("onboarding_mapper").joinpath("schemas", f"{name_or_path}.json")
        if not ref.is_file():
            raise SchemaError(f"no built-in schema '{name_or_path}' (available: {', '.join(builtin_names())})")
        data = ref.read_bytes()
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SchemaError(f"schema is not valid JSON: {exc}") from exc
    return from_dict(raw, hashlib.sha256(data).hexdigest())
