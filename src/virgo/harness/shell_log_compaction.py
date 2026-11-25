"""Branch-local, reversible projections of completed foreground Shell results."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, replace

from ..ai import request_model
from ..ai.compaction import context_binding
from ..ai.types import (
    AssistantMessage,
    RawContent,
    TextContent,
    ThinkingContent,
    ToolResultMessage,
    UserMessage,
)
from ..execution import FileAccessDenied
from .compaction import _chars, estimate_chars, estimate_tokens

RECORD_KIND = "shell_log_replacements"


@dataclass(frozen=True)
class ShellLogSettings:
    start_tokens: int
    reserve_tokens: int
    excerpt_chars: int

    @classmethod
    def parse(cls, value):
        if value is None:
            return None
        keys = ("startTokens", "reserveTokens", "excerptChars")
        if not isinstance(value, dict) or set(value) != set(keys) or any(
            type(value[key]) is not int or value[key] <= 0 for key in keys
        ):
            raise ValueError("compaction.shellLogs requires positive startTokens, reserveTokens, and excerptChars")
        return cls(*(value[key] for key in keys))


def foreground_success(details, is_error) -> bool:
    return (
        is_error is False and isinstance(details, dict) and details.get("status") == "ok"
        and type(details.get("exit_code")) is int and details["exit_code"] == 0
        and not details.get("cancelled")
        and isinstance(details.get("output_id"), str) and bool(details["output_id"])
    )


def _old_shell_results(messages) -> list[int]:
    batches = []
    expected = {}
    results = []
    has_batch = False
    for index, message in enumerate([*messages, None]):
        if not isinstance(message, ToolResultMessage):
            if has_batch and not expected:
                batches.append(results)
            expected = {}
            results = []
            has_batch = False
            if isinstance(message, AssistantMessage) and message.stop_reason not in {"aborted", "error", "compaction"}:
                calls = message.tool_calls()
                if len({call.id for call in calls}) == len(calls):
                    expected = {call.id: call for call in calls}
                    has_batch = bool(expected)
            continue
        call = expected.pop(message.tool_call_id, None)
        if call is None or call.name != message.tool_name:
            # A malformed batch must never yield candidates.
            expected.clear()
            results = []
            has_batch = False
            continue
        details = message.details
        if foreground_success(details, message.is_error) and details.get("foreground_shell_output") is True:
            if isinstance(call.arguments, dict) and not call.arguments.get("background") and message.content and all(
                isinstance(block, TextContent) and not (block.native or block.citations)
                for block in message.content
            ):
                results.append(index)
    return [index for batch in batches[:-1] for index in batch]


def _protected(message, model) -> bool:
    if not isinstance(message, AssistantMessage):
        return False
    if any(isinstance(block, RawContent) and block.api == model.api for block in message.content):
        return True
    return message.api == model.api and message.provider in {"", model.provider} and any(
        isinstance(block, ThinkingContent) and (block.signature or block.redacted)
        or isinstance(block, TextContent) and block.native
        for block in message.content
    )


@dataclass
class Projection:
    messages: list
    versions: dict
    unavailable: list
    estimate: int | None = None
    decision: dict | None = None

    def request_messages(self):
        if not self.unavailable:
            return self.messages
        return [*self.messages, UserMessage("\n".join(self.unavailable), kind="notification")]


class ShellLogHistory:
    def __init__(self, runtime):
        self.runtime = runtime
        self.store = runtime.store
        self.has_records = bool(self.store and self.store.custom(RECORD_KIND))

    def project(self, context, model, *, settings=None, threshold=None, measure=False):
        """Rebuild the projection, then optionally write new replacements for this branch."""
        rt = self.runtime
        originals = context.messages
        if not self.store or (settings is None and not self.has_records and not measure):
            return Projection(originals, {}, [])
        model = request_model(model, context)
        source = {id(message): key for message, key in zip(rt.messages, rt.ids, strict=False)}
        ids = [source.get(id(message)) for message in originals]
        counts = Counter(ids)
        records = {}
        for group in self.store.custom(RECORD_KIND) if self.has_records else ():
            if isinstance(group, dict):
                records.update(group)
        binding = context_binding(model, context) if measure else None
        overhead = context.system + (json.dumps(context.web_tools, ensure_ascii=False) if context.web_tools else "")
        metadata = {"binding": binding, "api": model.api, "versions": {}}
        if not records and measure:
            valid_from = self._valid_from(originals, ids, metadata)
            estimate = estimate_tokens(originals, valid_from, system=overhead, tools=context.tools)
            if settings is None or estimate < settings.start_tokens:
                rt._request_shell_context = metadata
                return Projection(originals, {}, [], estimate=estimate)
        replacements, versions, notices = list(originals), {}, []
        presented = {
            line for message in originals if isinstance(message, UserMessage) and message.kind == "notification"
            for line in message.text().splitlines()
        }
        candidate_indices = _old_shell_results(originals)
        candidate_set = set(candidate_indices)
        protected = set()
        signed_versions = None
        for index in range(len(originals) - 1, -1, -1):
            message, key = originals[index], ids[index]
            if _protected(message, model):
                metadata = self.store.by_id.get(key, {}).get("shell_log_context", {})
                signed_versions = metadata.get("versions", {})
            if index not in candidate_set or key is None or counts[key] != 1:
                continue
            if signed_versions is not None:
                protected.add(index)
            record = records.get(key)
            if not self._matches(record, message):
                continue
            oid = record["output_id"]
            available = self._readable(oid)
            version = "short" if available else "unavailable"
            if signed_versions is not None:
                version = signed_versions.get(key)
                if not available and version != "unavailable" and record["notice"] not in presented:
                    notices.append(record["notice"])
            if version == "short":
                replacements[index] = replace(message, content=[TextContent(record["short"])])
            elif version == "unavailable":
                replacements[index] = replace(message, content=[TextContent(message.text() + "\n\n" + record["notice"])])
            if version in {"short", "unavailable"}:
                versions[key] = version

        projection = Projection(replacements, versions, list(reversed(notices)))
        if not measure:
            return projection
        usage_metadata = {"binding": binding, "api": model.api, "versions": versions}
        valid_from = self._valid_from(originals, ids, usage_metadata)
        current = estimate_tokens(projection.request_messages(), valid_from, system=overhead, tools=context.tools)
        projection.estimate = current
        if settings is not None and current >= settings.start_tokens:
            self._shorten(projection, originals, ids, counts, candidate_indices, protected, records,
                          overhead, context.tools, settings, threshold)
        rt._request_shell_context = {**usage_metadata, "versions": projection.versions}
        return projection

    def _valid_from(self, messages, ids, metadata):
        rt = self.runtime
        if len(messages) != len(rt.messages) or any(
            message is not original for message, original in zip(messages, rt.messages, strict=True)
        ):
            return len(messages)
        for index in range(len(messages) - 1, rt.compacted_at - 1, -1):
            message = messages[index]
            if not isinstance(message, AssistantMessage) or message.usage.input_total <= 0 or (
                message.stop_reason in {"error", "aborted", "compaction"}
            ):
                continue
            previous = self.store.by_id.get(ids[index], {}).get("shell_log_context")
            if previous is None:
                if not metadata["versions"] and not self.has_records:
                    return index
                return len(messages)
            prefix_ids = set(ids[:index])
            previous_versions = {key: version for key, version in previous["versions"].items() if key in prefix_ids}
            current_versions = {key: version for key, version in metadata["versions"].items() if key in prefix_ids}
            if message.input_usage_reported is True and previous_versions == current_versions and all(
                previous[k] == metadata[k] for k in ("api", "binding")
            ):
                return index
            return len(messages)
        return len(messages)

    @staticmethod
    def _matches(record, message):
        return (
            isinstance(record, dict) and record.get("output_id") == message.details["output_id"]
            and record.get("call_id") == message.tool_call_id and record.get("tool") == message.tool_name
            and isinstance(record.get("short"), str) and len(record["short"].encode()) < len(message.text().encode())
            and isinstance(record.get("notice"), str)
        )

    def _readable(self, output_id):
        outputs = self.runtime.execution_context.outputs
        try:
            with outputs.open_read(outputs.lookup(output_id)):
                return True
        except (FileAccessDenied, OSError, ValueError):
            return False

    def _shorten(self, projection, originals, ids, counts, indices, protected, records,
                 overhead, tools, settings, threshold):
        chars = estimate_chars(projection.request_messages(), system=overhead, tools=tools)
        before = after = chars // 4
        selected = {}
        candidates = []
        t = self.runtime.session.t
        if threshold is not None and settings.start_tokens <= threshold and any(tool.name == "read_output" for tool in tools):
            for index in indices:
                message, key = originals[index], ids[index]
                if index in protected or key is None or counts[key] != 1 or key in records:
                    continue
                oid = message.details["output_id"]
                original = message.text()
                short = t("harness.compact.shell_short", excerpt=original[-settings.excerpt_chars:], output_id=oid)
                if len(short.encode()) >= len(original.encode()) or not self._readable(oid):
                    continue
                candidates.append((index, key, short))
            target = max(0, threshold - settings.reserve_tokens)
            for index, key, short in candidates:
                # The active estimate can use real usage. Only switch entirely to
                # the local estimate once a replacement invalidates that sample.
                if (after if selected else projection.estimate) <= target:
                    break
                original = originals[index]
                oid = original.details["output_id"]
                selected[key] = {
                    "output_id": oid, "call_id": original.tool_call_id, "tool": original.tool_name,
                    "short": short, "notice": t("harness.compact.shell_unavailable", output_id=oid),
                }
                projection.messages[index] = replace(original, content=[TextContent(short)])
                projection.versions[key] = "short"
                chars += len(short) - _chars(original)
                after = chars // 4
        if selected:
            self.store.append("custom", kind=RECORD_KIND, data=selected)
            self.has_records = True
            projection.estimate = after
        projection.decision = {
            "layer": 2, "reason": "start_watermark", "candidate_count": len(candidates),
            "shortened_count": len(selected), "estimated_before": before, "estimated_after": after,
            "estimated_for_decision": projection.estimate, "threshold": threshold,
            "start_tokens": settings.start_tokens, "reserve_tokens": settings.reserve_tokens,
        }
