"""Bounded construction context and correction records for diagnostic probes."""

from __future__ import annotations

from copy import deepcopy
import json

from .runtime import TimeLimitError


MAX_GUIDANCE_CHARS = 8000
MAX_FEEDBACK_CHARS = 4000


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _preview(value, limit: int):
    try:
        text = _json(value)
    except (ValueError, TypeError, RecursionError):
        return {"json_preview": "[invalid JSON value]", "truncated": True}
    return deepcopy(value) if len(text) <= limit else {"json_preview": text[:limit], "truncated": True}


def bounded_feedback(value: dict | None, *, max_chars: int = MAX_FEEDBACK_CHARS) -> dict | None:
    if not isinstance(value, dict):
        return None
    error = value.get("error") if isinstance(value.get("error"), dict) else {}
    request = value.get("request") if isinstance(value.get("request"), dict) else {}
    output = {"source_hash": value.get("source_hash"), "accepted_source_hash": value.get("accepted_source_hash"),
              "requested_base": str(request.get("base", value.get("requested_base", "")))[:256],
              "request": _preview(request, 1000),
              "error": {"code": str(error.get("code", "diagnostic_incomplete"))[:80],
                        "message": str(error.get("message", ""))[:400]}}
    for key in ("allowed_keys", "required_keys"):
        if isinstance(error.get(key), list):
            output["error"][key] = [str(item)[:100] for item in error[key][:16]]
    if "duplicate_reference" in error:
        output["error"]["duplicate_reference"] = _preview(error["duplicate_reference"], 400)
    conflicts = error.get("conflicts", [])
    if isinstance(conflicts, list):
        output["error"]["conflicts"] = deepcopy([row for row in conflicts[:12] if isinstance(row, dict)])
        total = error.get("conflicts_total", len(conflicts))
        output["error"]["conflicts_total"] = total if type(total) is int and 0 <= total <= 10000 else len(conflicts)
        output["error"]["conflicts_omitted"] = max(0, output["error"]["conflicts_total"] - len(output["error"]["conflicts"]))
    replay = value.get("accepted_replay")
    if isinstance(replay, dict):
        output["accepted_replay"] = {key: str(replay.get(key, ""))[:300]
                                     for key in ("run_id", "source_hash", "status", "error")}
    while len(_json(output)) > max_chars and output["error"].get("conflicts"):
        output["error"]["conflicts"].pop()
        output["error"]["conflicts_omitted"] += 1
    if len(_json(output)) > max_chars:
        output.pop("request", None)
        output["details_truncated"] = True
    if len(_json(output)) > max_chars:
        output = {"source_hash": value.get("source_hash"), "accepted_source_hash": value.get("accepted_source_hash"),
                  "error": {"code": str(error.get("code", "diagnostic_incomplete"))[:80],
                            "message": str(error.get("message", ""))[:200]}, "details_truncated": True}
    return output


def correction_record(source_hash: str, accepted_hash: str, request: dict, result: dict, measured: dict) -> dict | None:
    if measured.get("complete"):
        return None
    error = result.get("error") if not result.get("ok") else {
        "code": "diagnostic_incomplete", "message": measured.get("reason", "probe did not complete")}
    if isinstance(error, dict) and measured.get("duplicate_reference") is not None:
        error = {**error, "code": "duplicate_configuration", "duplicate_reference": measured["duplicate_reference"]}
    return bounded_feedback({"source_hash": source_hash, "accepted_source_hash": accepted_hash,
                             "request": request, "error": error,
                             "accepted_replay": measured.get("before", measured.get("observation"))})


def _editable_values(document: dict) -> list[dict]:
    values = []
    scanned = 0

    def visit(value, path):
        nonlocal scanned
        scanned += 1
        if scanned > 256 or len(values) >= 12:
            return
        if isinstance(value, dict):
            keys = sorted(value, key=lambda key: (key != "faults", key))
            for key in keys:
                if not path and key in {"name", "expect", "seed"}:
                    continue
                visit(value[key], [*path, key])
        elif path:
            row = {"path": path, "value": _preview(value, 192),
                   "override_kind": "whole_list" if isinstance(value, list) else "value"}
            if len(_json(row)) <= 700:
                values.append(row)

    visit(document, [])
    return values


def construction_guidance(reader, failures: list[dict], previous: dict | None,
                          source_hash: str, accepted_hash: str) -> str:
    """Reuse protected composition reads; never import service code or choose a fault."""
    current_failure = (previous if isinstance(previous, dict)
                       and previous.get("source_hash") == source_hash
                       and previous.get("accepted_source_hash") == accepted_hash else None)
    references = []
    requested = current_failure.get("requested_base") if current_failure else None
    if isinstance(requested, str) and requested:
        references.append(requested)
    for row in failures:
        if (isinstance(row, dict) and row.get("source_hash") in {source_hash, accepted_hash}
                and isinstance(row.get("scenario"), str) and row["scenario"] not in references):
            references.append(row["scenario"])
        if len(references) >= 2:
            break
    if not references:
        references = ["public"]
    bases = []
    for reference in references[:2]:
        try:
            path = reader._composition_path(reference)
            document, metadata = reader._composition_document(path, reference, reader.reads)
            inherited = []
            document = reader._inline_composition_faults(document, path, reader.reads, inherited)
            row = {"base": reference, "file_sha256": metadata.get("sha256"),
                   "inherited_fault_files": len(inherited), "resolved_document": document,
                   "document_truncated": False}
            if len(_json(row)) > 2800:
                row.pop("resolved_document")
                row.update(document_truncated=True, document_preview=_json(document)[:800],
                           top_level_keys=[key for key in sorted(document) if len(key) <= 100][:24],
                           editable_values=_editable_values(document), editable_values_complete=False)
                while len(_json(row)) > 2800 and row["editable_values"]:
                    row["editable_values"].pop()
                if len(_json(row)) > 2800:
                    row.pop("document_preview", None)
            bases.append(row)
        except TimeLimitError:
            raise
        except (AttributeError, ValueError, TypeError, OSError, RuntimeError, RecursionError) as error:
            bases.append({"base": reference, "available": False, "error": str(error)[:300]})
    context = {"source_hash": source_hash, "accepted_source_hash": accepted_hash,
               "bases": bases, "previous_construction_failure": bounded_feedback(current_failure),
               "instructions": "Use the documented scenario schema and one small runtime change. "
                               "The controller supplies each replay seed; do not add a top-level seeds list. "
                               "Resolve every reported conflict with exact nested overrides. fault_profiles merges; "
                               "overrides.faults with a shipped reference replaces the fault block. "
                               "Truncated previews are not complete configurations; preserve workload and expectations."}
    while len(_json(context)) > MAX_GUIDANCE_CHARS and len(context["bases"]) > 1:
        context["bases"].pop()
    if len(_json(context)) > MAX_GUIDANCE_CHARS:
        context["bases"] = [{"base": row["base"], "details_truncated": True} for row in context["bases"]]
    return _json(context)
