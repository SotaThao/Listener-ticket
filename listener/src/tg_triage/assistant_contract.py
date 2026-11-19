"""Wire schema and pure validation for assistant decisions."""
from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Protocol

from . import issue_format
from .decision_rules import normalize
from .models import IncomingMessage, utf8_valid

__all__ = (
    "CONTRACT", "PHASE_DECIDE", "STATES", "GUARDS", "PLAN_KEYS_DECIDE",
    "Decision", "GroupDecision", "Violation", "GuardContext", "DecideRuntime",
    "json_schema", "parse", "to_plan",
    "guard_coverage", "guard_state", "guard_missing_field", "guard_repo",
    "guard_target", "guard_channel", "guard_grounding", "guard_image_split",
    "guard_document", "guard_sanitize", "guard_provenance", "guard_unknown_rule",
)

CONTRACT = "decide/v1"
PHASE_DECIDE = "assistant_decide"
STATES = frozenset({
    "READY", "APPEND", "APPEND_CLOSED", "NEED_INFO", "CHAT", "MEDIA_ONLY",
    "HOLD", "ANALYZE", "STATUS", "LIFECYCLE_QUESTION", "RELATED",
})
GUARDS = (
    "guard_coverage", "guard_state", "guard_missing_field", "guard_repo",
    "guard_target", "guard_channel", "guard_grounding", "guard_image_split",
    "guard_document", "guard_sanitize", "guard_limits", "guard_provenance",
    "guard_prefilter", "guard_unknown_rule",
)
PLAN_KEYS_DECIDE = ("authority", "rule_text", "question_block", "refs", "run_id", "related")

# The ticket fields are the text fields in facts.md. The template carries
# intent, and attachment selection is executor-owned rather than an issue field.
_TEXT_FIELDS = ("object", "observed", "expected", "work", "problem", "proposal")
_APPEND = {"APPEND": "open", "APPEND_CLOSED": "closed"}
_SEARCH_RELATED = "search target may only be related"
_PHASE_ONE_BLOCKED = frozenset({"ANALYZE", "RELATED", "LIFECYCLE_QUESTION", "APPEND_CLOSED"})
DEFAULT_QUESTION_BLOCK = "question.fallback"


def _object(properties, *, required=None):
    return {"type": "object", "properties": properties, "additionalProperties": False,
            "required": list(properties) if required is None else list(required)}


def _nullable(schema):
    return {"anyOf": [schema, {"type": "null"}]}


def json_schema() -> dict:
    """Return a fresh JSON Schema for the decide/v1 response object."""
    string = {"type": "string"}
    nonempty = {"type": "string", "minLength": 1}
    number = {"type": "integer", "minimum": 1}
    ids = {"type": "array", "items": number, "minItems": 1}
    ticket_field = _object({"value": string, "source_ids": ids})
    fields = _object(dict.fromkeys(_TEXT_FIELDS, ticket_field), required=())
    group = _object({
        "message_ids": ids,
        "state": {"type": "string", "enum": sorted(STATES)},
        "rule": nonempty,
        "template": _nullable({"type": "string", "enum": ["bug", "task", "feedback"]}),
        "issue": _nullable(_object({"title": string, "fields": fields})),
        "target": _nullable(_object({"number": number, "basis": {
            "type": "string", "enum": ["reply", "search"],
        }})),
        "relation": _nullable({"type": "string", "enum": ["duplicate", "related"]}),
        "question": _nullable(_object({
            "block_id": nonempty,
            "fields": {"type": "array", "items": {"type": "string", "enum": list(_TEXT_FIELDS)}},
        })),
        "refs": {"type": "array", "items": _object({"number": number}), "maxItems": 5},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reasoning_summary": {"type": "string", "maxLength": 500},
    })
    return _object({"contract": {"type": "string", "enum": [CONTRACT]},
                    "groups": {"type": "array", "items": group, "minItems": 1}})


@dataclass(frozen=True)
class Decision:
    """One model decision, before trusted execution metadata is attached."""

    message_ids: tuple[int, ...]
    state: str
    rule: str
    template: str | None
    issue: Mapping | None
    target: Mapping | None
    relation: str | None
    question: Mapping | None
    refs: tuple[Mapping, ...]
    confidence: float
    reasoning_summary: str


@dataclass(frozen=True)
class GroupDecision(Decision):
    """A candidate group with runtime metadata from the trusted snapshot."""

    chat_id: int
    rule_text: str = ""
    run_id: int | None = None
    targets: tuple[Mapping, ...] = ()
    document_reads: tuple[Mapping, ...] = ()
    related: tuple[int, ...] = ()


@dataclass(frozen=True)
class Violation:
    guard: str
    group_index: int | None  # None identifies an error in the batch or envelope.
    detail: str
    blocking: bool = True
    repairable: bool = True


@dataclass(frozen=True)
class GuardContext:
    """Trusted data for this batch and model turn, with no I/O in rule_text.

    reply_targets maps the replying source message ID to (ticket number, state)
    in gh_repo. tool_targets maps numbers from this turn to states in gh_repo.
    tool_target_repos records the observed repository, or None for a number
    seen in multiple repositories. question_block_ids holds
    the blocks available to the question renderer. reply_proofs and
    document_reads may carry full executor records, keyed by source ID.
    """

    message_ids: frozenset[int]
    chat_id: int
    gh_repo: str
    reply_targets: Mapping[int, tuple[int, str]]
    tool_targets: Mapping[int, str]
    rule_ids: frozenset[str]
    rule_text: Callable[[str], str]
    question_block_ids: frozenset[str]
    max_value_chars: int = 200
    max_fields_per_ask: int = 3
    seen_image_ids: frozenset[int] = frozenset()
    read_document_ids: frozenset[int] = frozenset()
    append_confidence: float = 0.8
    tool_target_repos: Mapping[int, str | None] = field(default_factory=dict)
    reply_proofs: Mapping[int, Mapping] = field(default_factory=dict)
    document_reads: Mapping[int, Mapping] = field(default_factory=dict)


class DecideRuntime(Protocol):
    def decide(self, job: dict) -> tuple[list[GroupDecision] | None, int | None, str]:
        """Return (decisions, run_id, outcome): done, infra, model_error or guard."""
        ...


_JSON_TYPES = {
    "object": (dict,), "array": (list,), "string": (str,),
    "integer": (int,), "number": (int, float), "null": (type(None),),
}


def _schema_error(value, schema, path):
    """Validate the JSON Schema keywords emitted by json_schema."""
    if "anyOf" in schema:
        if value is None:
            return None
        return _schema_error(value, schema["anyOf"][0], path)
    kind = schema["type"]
    if type(value) not in _JSON_TYPES[kind]:
        return path, f"expected {kind}"
    if "enum" in schema and value not in schema["enum"]:
        return path, "value outside allowed set"
    if kind == "object":
        properties = schema["properties"]
        if value.keys() - properties.keys() or not value.keys() >= set(schema["required"]):
            return path, "missing or unexpected object properties"
        for key, child in value.items():
            if error := _schema_error(child, properties[key], (*path, key)):
                return error
    elif kind == "array":
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", len(value)):
            return path, "array length outside allowed range"
        for index, child in enumerate(value):
            if error := _schema_error(child, schema["items"], (*path, index)):
                return error
    elif kind == "string":
        if not utf8_valid(value):
            return path, "invalid UTF-8"
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", len(value)):
            return path, "string length outside allowed range"
    elif kind in ("integer", "number"):
        if not schema.get("minimum", value) <= value <= schema.get("maximum", value):
            return path, "number outside allowed range"
    return None


def _freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(child) for key, child in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(map(_freeze, value))
    return value


def _json_value(value):
    if isinstance(value, Mapping):
        return {key: _json_value(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return list(map(_json_value, value))
    return value


def guard_coverage(groups, batch, ctx):
    batch_ids = Counter(message.message_id for message in batch)
    if (batch_ids != Counter(set(ctx.message_ids))
            or any(message.chat_id != ctx.chat_id for message in batch)):
        return Violation("guard_coverage", None, "batch must match context IDs and chat")
    if Counter(mid for group in groups for mid in group.message_ids) != batch_ids:
        return Violation("guard_coverage", None, "groups must cover every batch ID exactly once")
    return None


def guard_state(group, ctx, index):
    if group.state not in STATES:
        return Violation("guard_state", index, "unsupported executor state")
    return None


def guard_missing_field(group, ctx, index):
    if group.state != "READY":
        return None
    fields = group.issue["fields"] if group.issue else {}
    present = {name for name, item in fields.items() if normalize(item["value"])}
    ready = {
        "bug": {"object", "observed"} <= present,
        "task": {"object", "work"} <= present,
        "feedback": bool({"problem", "proposal"} & present),
    }
    if not ready.get(group.template, False):
        return Violation("guard_missing_field", index, "READY needs a template and the R9–R11 fields")
    return None


def guard_repo(group, ctx, index):
    if group.state in {"READY", *_APPEND} and not ctx.gh_repo:
        return Violation("guard_repo", index, "missing write repository")
    if group.target is None:
        return None
    if not _tool_repo_matches(group.target["number"], ctx) or (
        group.target["basis"] == "reply" and any(
            not _reply_repo_matches(mid, ctx) for mid, _ in _reply_matches(group, ctx)
        )
    ):
        return Violation("guard_repo", index, "target repository is ambiguous or outside the write repository")
    return None


def _tool_repo_matches(number, ctx):
    repo = ctx.tool_target_repos.get(number, ctx.gh_repo)
    return bool(repo and repo.casefold() == ctx.gh_repo.casefold())


def _reply_repo_matches(mid, ctx):
    repo = ctx.reply_proofs.get(mid, {}).get("repo", ctx.gh_repo)
    return bool(repo and repo.casefold() == ctx.gh_repo.casefold())


def _filter_refs(group, ctx, index):
    replies = {proof[0] for mid in group.message_ids
               if (proof := ctx.reply_targets.get(mid)) and proof[1] in _APPEND.values()
               and _reply_repo_matches(mid, ctx)}
    refs = tuple(ref for ref in group.refs if _tool_repo_matches(ref["number"], ctx)
                 and (ref["number"] in replies or ctx.tool_targets.get(ref["number"]) in _APPEND.values()))
    dropped = len(group.refs) - len(refs)
    if dropped:
        return replace(group, refs=refs), Violation(
            "guard_target", index, f"dropped {dropped} unproven ticket reference(s)", blocking=False)
    return group, None


def guard_provenance(state, index):
    if state in _PHASE_ONE_BLOCKED:
        return Violation("guard_provenance", index,
                         f"{state} requires host provenance outside decide phase 1", repairable=False)
    return None


def _reply_matches(group, ctx):
    number = group.target["number"]
    return [(mid, state) for mid in group.message_ids
            if (proof := ctx.reply_targets.get(mid)) and proof[0] == number
            and (state := proof[1]) in ("open", "closed")]


def guard_target(group, ctx, index):
    expected = _APPEND.get(group.state)
    target = group.target
    if target is None:
        if expected:
            return Violation("guard_target", index, "APPEND needs a proven target")
        return None
    if target["basis"] == "reply":
        states = {state for _, state in _reply_matches(group, ctx)}
        if expected and any(proof[0] != target["number"] for mid in group.message_ids
                            if (proof := ctx.reply_targets.get(mid))):
            return Violation("guard_target", index, "conflicting reply targets in group")
    else:
        states = {ctx.tool_targets.get(target["number"])} & {"open", "closed"}
    if not states or (expected and states != {expected}):
        return Violation("guard_target", index, "target proof missing or ticket state mismatched")
    if expected and target["basis"] == "search" and (
        group.relation != "duplicate" or group.confidence < ctx.append_confidence
    ):
        return Violation("guard_target", index, _SEARCH_RELATED, blocking=False)
    return None


def _downgrade_search(group, ctx):
    related = (group.target["number"],)
    replies = {ctx.reply_targets[mid] for mid in group.message_ids if mid in ctx.reply_targets}
    if replies:
        if any(state == "closed" for _, state in replies):
            return None
        number = min(number for number, _ in replies)
        # The ordinary target guard below rejects conflicting reply proofs.
        return replace(group, state="APPEND",
                       rule="R6", target=_freeze({"number": number, "basis": "reply"}),
                       relation=None, related=related)
    group = replace(group, target=None, relation=None, related=related)
    ready_rule = {"bug": "R9", "task": "R10", "feedback": "R11"}.get(group.template)
    if guard_missing_field(replace(group, state="READY"), ctx, None) is None:
        return replace(group, state="READY", rule=ready_rule, question=None)
    return replace(group, state="NEED_INFO", rule="R12" if group.template else "R13")


def guard_channel(group, ctx, index):
    question = group.question
    if question and (question["block_id"] not in ctx.question_block_ids
                     or len(question["fields"]) > ctx.max_fields_per_ask):
        return Violation("guard_channel", index, "unknown question block or too many fields")
    return None


def guard_grounding(group, ctx, index, batch):
    if group.issue is None:
        return None
    ids = set(group.message_ids)
    available = {mid for mid in ids if batch[mid].text.strip()
                 or (batch[mid].has_image and mid in ctx.seen_image_ids)
                 or (batch[mid].has_document and mid in ctx.read_document_ids)}
    for name, item in group.issue["fields"].items():
        cited = set(item["source_ids"])
        if not cited <= ids or not cited <= available:
            return Violation("guard_grounding", index, f"{name} cites a foreign or unavailable source")
    return None


def guard_image_split(groups, batch):
    owner = {mid: index for index, group in enumerate(groups) for mid in group.message_ids}
    for message in batch.values():
        original = batch.get(message.reply_to_id)
        if (original and (message.has_image or original.has_image)
                and owner[message.message_id] != owner[original.message_id]):
            return Violation("guard_image_split", owner[message.message_id],
                             "image and its in-batch reply must stay together")
    return None


def guard_document(group, ctx, index, batch):
    if any(batch[mid].has_document and mid not in ctx.read_document_ids for mid in group.message_ids):
        return Violation("guard_document", index, "group contains an unread document")
    return None


def guard_sanitize(group, ctx):
    if group.issue is None:
        return group

    def clean(value):
        # Redact before cutting so the cutoff cannot expose a partial address
        # or phone number. Recheck after sanitizing removes control characters.
        bounded = issue_format.redact(value)[:ctx.max_value_chars]
        return issue_format.redact(issue_format.sanitize(bounded, inline=True))

    issue = _freeze({
        "title": clean(group.issue["title"]),
        "fields": {name: {"value": clean(item["value"]), "source_ids": item["source_ids"]}
                   for name, item in group.issue["fields"].items()},
    })
    return replace(group, issue=issue)


def guard_unknown_rule(group, ctx, index):
    if group.rule not in ctx.rule_ids:
        return group, Violation("guard_unknown_rule", index, "rule ID absent from loaded snapshot")
    text = ctx.rule_text(group.rule).encode("utf-8")[:2048].decode("utf-8", errors="ignore")
    return replace(group, rule_text=text), None


def _targets(group, ctx, batch):
    target = group.target
    if target is None:
        return ()
    number = target["number"]
    if target["basis"] == "search":
        return (_freeze({"number": number, "state": ctx.tool_targets[number], "basis": "search"}),)
    return tuple(_freeze(ctx.reply_proofs.get(mid, {
        "number": number, "state": state, "chat_id": group.chat_id,
        "reply_message_id": mid, "reply_to_id": batch[mid].reply_to_id,
    })) for mid, state in _reply_matches(group, ctx))


def _unique_object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate JSON keys")
    return result


def parse(text_or_obj, *, batch: Sequence[IncomingMessage], ctx: GuardContext
          ) -> tuple[list[GroupDecision], list[Violation]]:
    """Validate the whole response and return candidates plus violations.

    Malformed responses yield no candidates. Only blocking violations prevent
    execution. An uncertain but proven search target becomes a related ticket
    and the replacement decision goes through the ordinary guards.
    """
    try:
        obj = (json.loads(text_or_obj, object_pairs_hook=_unique_object)
               if isinstance(text_or_obj, str) else text_or_obj)
    except (ValueError, RecursionError):
        return [], [Violation("schema", None, "invalid JSON decision")]
    if error := _schema_error(obj, json_schema(), ()):
        path, detail = error
        index = path[1] if len(path) >= 2 and path[0] == "groups" else None
        # State is both a wire enum and an executor guard, so give it the
        # same diagnostic when it is rejected at the schema boundary.
        guard = "guard_state" if len(path) == 3 and path[2] == "state" else "schema"
        location = ".".join(map(str, path)) or CONTRACT
        return [], [Violation(guard, index, f"{location}: {detail}")]

    groups = [GroupDecision(**{key: _freeze(value) for key, value in group.items()},
                            chat_id=ctx.chat_id) for group in obj["groups"]]
    if error := guard_coverage(groups, batch, ctx):
        return [], [error]
    messages = {message.message_id: message for message in batch}
    errors = []
    if error := guard_image_split(groups, messages):
        errors.append(error)
    for index, group in enumerate(groups):
        group = guard_sanitize(group, ctx)
        group, rule_error = guard_unknown_rule(group, ctx, index)
        group, refs_note = _filter_refs(group, ctx, index)
        if refs_note:
            errors.append(refs_note)
        provenance_error = guard_provenance(group.state, index)
        if provenance_error:
            errors.append(provenance_error)
            groups[index] = group
            continue
        repo_error = guard_repo(group, ctx, index)
        target_error = guard_target(group, ctx, index)
        channel_error = guard_channel(group, ctx, index)
        if not repo_error and not rule_error and target_error and not target_error.blocking:
            errors.append(target_error)
            downgraded = _downgrade_search(group, ctx)
            if downgraded is None:
                errors.append(guard_provenance("APPEND_CLOSED", index))
                groups[index] = group
                continue
            group = downgraded
            repo_error = guard_repo(group, ctx, index)
            target_error = guard_target(group, ctx, index)
            group, rule_error = guard_unknown_rule(group, ctx, index)
        errors.extend(error for error in (
            guard_state(group, ctx, index), guard_missing_field(group, ctx, index),
            repo_error, target_error, channel_error,
            guard_grounding(group, ctx, index, messages),
            guard_document(group, ctx, index, messages), rule_error,
        ) if error is not None)
        targets = ()
        if not repo_error and group.target and not target_error:
            targets = _targets(group, ctx, messages)
        if group.state == "NEED_INFO" and group.question is None:
            group = replace(group, question=_freeze({"block_id": DEFAULT_QUESTION_BLOCK, "fields": []}))
        groups[index] = replace(group, targets=targets, document_reads=tuple(
            _freeze(ctx.document_reads[mid]) for mid in sorted(group.message_ids)
            if mid in ctx.read_document_ids and mid in ctx.document_reads
        ))
    return groups, errors


def to_plan(group: GroupDecision, *, rules_fingerprint: str) -> dict:
    """Map a guarded group for save_batch(batch_id, rules_fingerprint, plans).

    The fingerprint is stored on the batch header by save_batch.
    """
    # Legacy or decide key(s)              | Source
    # source_keys                          | Trusted chat ID and model group IDs
    # state, intent, intent_rule, rule      | State, template, "assistant", snapshot rule ID
    # present                              | Clean ticket fields with source IDs
    # questions, asked_fields              | Selected block (0 or 1) and requested fields
    # task, analysis_review, analysis_origin | No analysis task/proof in decide/v1
    # lifecycle, lifecycle_*               | Trusted lifecycle data supplied by intake
    # targets                              | Validated reply proofs, or tagged search proof for APPEND*
    # pending_topic, pending_reply_ids     | Phase 1 uses the existing follow-up path
    # document_reads                       | Trusted read records for this group
    # authority, rule_text                 | "decide" and snapshot rule text
    # question_block                       | Selected block ID or None
    # refs, run_id                         | Ticket references and runtime-assigned run ID
    # related                              | Proven target number when used as a relation
    # The legacy READY path derives its title from present. The model's title
    # stays on GroupDecision, along with confidence and reasoning_summary.
    block = (group.question["block_id"] if group.question else
             DEFAULT_QUESTION_BLOCK if group.state == "NEED_INFO" else None)
    fields = group.issue["fields"] if group.issue else {}
    related = (group.related[0] if group.related else
               group.target["number"] if group.target and group.targets
               and group.state not in _APPEND and group.relation else None)
    targets = [proof for proof in group.targets
               if proof.get("basis") != "search" or group.state in _APPEND]
    return dict(
        source_keys=[[group.chat_id, mid] for mid in group.message_ids],
        state=group.state, intent=group.template, intent_rule="assistant", rule=group.rule,
        present=_json_value(fields), questions=[block] if block else [],
        asked_fields=list(group.question["fields"]) if group.question else [],
        task=None, analysis_review=None, analysis_origin=None,
        lifecycle=None, lifecycle_assets=None, lifecycle_sources=None,
        lifecycle_pixels=None, lifecycle_context=None, lifecycle_question=None,
        lifecycle_evidence_answer=None, lifecycle_recurrence_answer=None,
        lifecycle_source_evidence=None, targets=[_json_value(proof) for proof in targets],
        pending_topic=None, pending_reply_ids=[], document_reads=_json_value(group.document_reads),
        authority="decide", rule_text=group.rule_text, question_block=block,
        refs=_json_value(group.refs), run_id=group.run_id, related=related,
    )
