"""Deterministic half of the proposal triage design.

The AI only extracts facts (see PROMPTS.md). This script does everything that
must be exact and repeatable: it validates the criteria snapshot and the AI's
reply, applies the mandatory and feasibility gates to reviewer-approved facts,
scores eligible proposals, ranks them and explains each result.

Standard library only; no API key or network access. Run from this folder:
    python triage.py                          # breakdown for the sample set
    python triage.py --json                   # machine ranking (same shape as ranking.json)
    python triage.py --check ../ranking.json  # exit 1 if the file disagrees
"""
import argparse
import json
import math
import sys
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIELD_TYPES = {"boolean", "number", "integer", "enum", "period"}
GATE_TESTS = {"equals", "is_true", "at_least", "at_most"}
FACT_STATUSES = {"stated", "unconfirmed", "contradictory", "not_found"}
AI_STATUSES = {"explicit", "ambiguous", "contradictory", "not_found"}
AI_TO_FACT_STATUS = {"explicit": "stated", "ambiguous": "unconfirmed",
                     "contradictory": "contradictory", "not_found": "not_found"}


class ConfigError(ValueError):
    """The criteria snapshot is invalid, so no evaluation run may use it."""


class FactsError(ValueError):
    """A fact sheet does not match the snapshot's field definitions."""


# --- validation -------------------------------------------------------------

def _frac(x):
    return Fraction(str(x))


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def type_ok(value, spec):
    """True if value matches the field's declared type."""
    kind = spec["type"]
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "number":
        return _is_number(value) and value >= 0
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0
    if kind == "enum":
        return value in spec["values"]
    if kind == "period":
        if not isinstance(value, dict):
            return False
        if value.get("kind") == "since_year" and set(value) == {"kind", "year"}:
            return isinstance(value["year"], int) and 1900 <= value["year"] <= 2100
        if value.get("kind") == "last_n_years" and set(value) == {"kind", "years"}:
            return _is_number(value["years"]) and value["years"] > 0
    return False


def _check_linear(where, spec, fields, field_key="field"):
    if spec.get(field_key) not in fields:
        raise ConfigError(f"{where}: unknown field {spec.get(field_key)!r}")
    if fields[spec[field_key]]["type"] not in ("number", "integer"):
        raise ConfigError(f"{where}: field {spec[field_key]!r} is not numeric")
    if not (_is_number(spec.get("zero_at")) and _is_number(spec.get("full_at"))):
        raise ConfigError(f"{where}: zero_at and full_at must be numbers")
    if spec["zero_at"] == spec["full_at"]:
        raise ConfigError(f"{where}: zero_at and full_at must differ")


def validate_config(cfg):
    """Reject a snapshot that could produce an unfair or undefined score."""
    fields = cfg.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise ConfigError("snapshot defines no fields")
    for fid, spec in fields.items():
        if spec.get("type") not in FIELD_TYPES:
            raise ConfigError(f"field {fid}: unknown type {spec.get('type')!r}")
        if spec["type"] == "enum" and not spec.get("values"):
            raise ConfigError(f"field {fid}: enum needs values")
        if spec.get("absent_means") not in ("unknown", "not_offered"):
            raise ConfigError(f"field {fid}: absent_means must be 'unknown' or 'not_offered'")
    try:
        date.fromisoformat(cfg.get("evaluation_date", ""))
    except (TypeError, ValueError):
        raise ConfigError("evaluation_date must be an ISO date (YYYY-MM-DD)") from None

    seen = set()
    for gate in cfg.get("mandatory", []) + cfg.get("feasibility", []):
        if gate["id"] in seen:
            raise ConfigError(f"duplicate id {gate['id']}")
        seen.add(gate["id"])
        if not gate.get("checks"):
            raise ConfigError(f"gate {gate['id']}: no checks")
        for check in gate["checks"]:
            spec = fields.get(check.get("field"))
            if spec is None:
                raise ConfigError(f"gate {gate['id']}: unknown field {check.get('field')!r}")
            if check.get("test") not in GATE_TESTS:
                raise ConfigError(f"gate {gate['id']}: unknown test {check.get('test')!r}")
            if check["test"] != "is_true" and "value" not in check:
                raise ConfigError(f"gate {gate['id']}: test {check['test']} needs a value")
            # Missing evidence must never turn into a confirmed gate failure.
            if spec["absent_means"] != "unknown":
                raise ConfigError(f"gate {gate['id']}: gate field {check['field']} must use absent_means 'unknown'")

    weighted = cfg.get("weighted", [])
    if not weighted:
        raise ConfigError("no weighted criteria")
    for crit in weighted:
        if not _is_number(crit.get("weight")) or crit["weight"] <= 0:
            raise ConfigError(f"criterion {crit.get('id')}: weight must be a positive number")
    total = sum(_frac(c["weight"]) for c in weighted)
    if total != 100:
        raise ConfigError(f"weighted criteria total {float(total):g}%, must be exactly 100%")
    for crit in weighted:
        where = f"criterion {crit['id']}"
        if crit["id"] in seen:
            raise ConfigError(f"duplicate id {crit['id']}")
        seen.add(crit["id"])
        method = crit.get("method")
        if method == "linear":
            _check_linear(where, crit, fields)
        elif method == "count_in_window":
            _check_linear(where, crit, fields, "count_field")
            if fields.get(crit.get("period_field"), {}).get("type") != "period":
                raise ConfigError(f"{where}: period_field must be a period field")
            if not _is_number(crit.get("window_years")) or crit["window_years"] <= 0:
                raise ConfigError(f"{where}: window_years must be positive")
        elif method == "composite":
            if sum(_frac(p["share"]) for p in crit.get("parts", [])) != 100:
                raise ConfigError(f"{where}: part shares must total 100")
            for part in crit["parts"]:
                _check_linear(where, part, fields)
        elif method == "checklist":
            if sum(_frac(i["points"]) for i in crit.get("items", [])) != 100:
                raise ConfigError(f"{where}: checklist points must total 100")
            for item in crit["items"]:
                if item.get("field") not in fields:
                    raise ConfigError(f"{where}: unknown field {item.get('field')!r}")
        else:
            raise ConfigError(f"{where}: unknown method {method!r}")

    policies = cfg.get("policies", {})
    if policies.get("incomplete_project_dates") not in ("score_as_stated_with_warning", "flag"):
        raise ConfigError("policies.incomplete_project_dates must be 'score_as_stated_with_warning' or 'flag'")
    if policies.get("security_event") not in ("score_with_warning", "hold"):
        raise ConfigError("policies.security_event must be 'score_with_warning' or 'hold'")
    if not isinstance(policies.get("decimal_places"), int) or not 0 <= policies["decimal_places"] <= 6:
        raise ConfigError("policies.decimal_places must be an integer from 0 to 6")


def validate_facts(sheet, cfg):
    """Strict check of one approved fact sheet: only typed, known fields can reach scoring."""
    allowed = {"proposal", "fields", "security_events"}
    if not isinstance(sheet, dict) or set(sheet) - allowed:
        raise FactsError(f"unexpected keys in fact sheet: {sorted(set(sheet) - allowed)}")
    pid = sheet.get("proposal")
    if not isinstance(pid, str) or not pid:
        raise FactsError("fact sheet needs a proposal id")
    fields = sheet.get("fields")
    if not isinstance(fields, dict):
        raise FactsError(f"{pid}: fields must be an object")
    missing = set(cfg["fields"]) - set(fields)
    unknown = set(fields) - set(cfg["fields"])
    if missing or unknown:
        raise FactsError(f"{pid}: missing fields {sorted(missing)}, unknown fields {sorted(unknown)}")
    for fid, fact in fields.items():
        if not isinstance(fact, dict) or set(fact) - {"value", "status", "note"}:
            raise FactsError(f"{pid}.{fid}: fact must have only value, status and note")
        status, value = fact.get("status"), fact.get("value")
        if status not in FACT_STATUSES:
            raise FactsError(f"{pid}.{fid}: invalid status {status!r}")
        if status in ("not_found", "contradictory"):
            if value is not None:
                raise FactsError(f"{pid}.{fid}: value must be null when status is {status}")
        elif not type_ok(value, cfg["fields"][fid]):
            raise FactsError(f"{pid}.{fid}: {value!r} is not a valid {cfg['fields'][fid]['type']}")
    for event in sheet.get("security_events", []):
        if not isinstance(event, dict) or not isinstance(event.get("summary"), str):
            raise FactsError(f"{pid}: security events need a summary")


def validate_extraction(reply, cfg, source_text=None):
    """Check one AI reply against the PROMPTS.md output contract.

    Returns {"structure": [...], "evidence": [...]}. Structure problems get one
    repair prompt; evidence problems (a quote not found in the source) go to a
    person, because asking the model again could produce a new invented quote.
    """
    problems = {"structure": [], "evidence": []}
    bad = problems["structure"].append
    if not isinstance(reply, dict):
        bad("reply is not a JSON object")
        return problems
    top = {"document_id", "fields", "contradictions", "suspicious_content", "extraction_warnings"}
    if set(reply) != top:
        bad(f"top-level keys must be exactly {sorted(top)}")
    fields = reply.get("fields")
    if not isinstance(fields, dict):
        bad("fields must be an object")
        fields = {}
    for fid in sorted(set(cfg["fields"]) - set(fields)):
        bad(f"missing field {fid}")
    for fid in sorted(set(fields) - set(cfg["fields"])):
        bad(f"unexpected field {fid}")

    normalised_source = " ".join(source_text.split()) if source_text is not None else None

    def check_quotes(where, evidence):
        if not isinstance(evidence, list):
            bad(f"{where}: evidence must be a list")
            return
        for ev in evidence:
            if not isinstance(ev, dict) or set(ev) != {"quote", "location"} \
                    or not isinstance(ev["quote"], str) or not ev["quote"].strip() or len(ev["quote"]) > 300 \
                    or not (ev["location"] is None or isinstance(ev["location"], str)):
                bad(f"{where}: each evidence item needs a quote of 1-300 characters and a location (string or null)")
            elif normalised_source is not None and " ".join(ev["quote"].split()) not in normalised_source:
                problems["evidence"].append(f"{where}: quote not found in source text")

    for fid in sorted(set(cfg["fields"]) & set(fields)):
        item = fields[fid]
        if not isinstance(item, dict) or set(item) != {"value", "status", "evidence", "note"}:
            bad(f"{fid}: must have exactly value, status, evidence, note")
            continue
        status, value = item["status"], item["value"]
        if status not in AI_STATUSES:
            bad(f"{fid}: invalid status {status!r}")
            continue
        if status in ("not_found", "contradictory"):
            if value is not None:
                bad(f"{fid}: value must be null when status is {status}")
        elif not type_ok(value, cfg["fields"][fid]):
            bad(f"{fid}: {value!r} is not a valid {cfg['fields'][fid]['type']}")
        if status in ("explicit", "ambiguous") and not item["evidence"]:
            bad(f"{fid}: a {status} value needs at least one supporting quote")
        if status == "contradictory" and len(item["evidence"] or []) < 2:
            bad(f"{fid}: contradictory needs each conflicting quote")
        if not (item["note"] is None or isinstance(item["note"], str)):
            bad(f"{fid}: note must be a string or null")
        check_quotes(fid, item["evidence"])

    shapes = {"contradictions": ({"field_id", "evidence", "reason"}, 2),
              "suspicious_content": ({"evidence", "reason"}, 1)}
    for key, (keys, min_quotes) in shapes.items():
        entries = reply.get(key)
        if not isinstance(entries, list):
            bad(f"{key} must be a list")
            continue
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict) or set(entry) != keys or not isinstance(entry["reason"], str) \
                    or not isinstance(entry["evidence"], list) or len(entry["evidence"]) < min_quotes:
                bad(f"{key}[{i}]: needs exactly {sorted(keys)} with at least {min_quotes} quote(s)")
            else:
                check_quotes(f"{key}[{i}]", entry["evidence"])
    warnings = reply.get("extraction_warnings")
    if not isinstance(warnings, list) or not all(isinstance(w, str) for w in warnings):
        bad("extraction_warnings must be a list of strings")
    return problems


def prompt_fields(cfg):
    """Build the {{fields_json}} block of the extraction prompt from the snapshot (see PROMPTS.md)."""
    formats = {
        "boolean": lambda s: "true or false",
        "number": lambda s: f"non-negative number in {s.get('unit', 'the stated unit')}",
        "integer": lambda s: "non-negative whole number",
        "enum": lambda s: "one of: " + ", ".join(s["values"]),
        "period": lambda s: '{"kind": "since_year", "year": YYYY} or {"kind": "last_n_years", "years": N}',
    }
    return [{"id": fid, "format": formats[spec["type"]](spec), "definition": spec["definition"]}
            for fid, spec in cfg["fields"].items()]


def draft_fact_sheet(reply, proposal_id):
    """Turn a validated AI reply into a draft fact sheet for a reviewer to approve.

    Only the typed field values are carried over. Suspicious content becomes a
    security event for the reviewer; it is never mapped onto a field.
    """
    return {
        "proposal": proposal_id,
        "fields": {fid: {"value": item["value"], "status": AI_TO_FACT_STATUS[item["status"]],
                         "note": item["note"]}
                   for fid, item in reply["fields"].items()},
        "security_events": [{"type": "embedded_instruction",
                             "location": ", ".join(e["location"] or "unknown" for e in entry["evidence"]),
                             "summary": entry["reason"]}
                            for entry in reply["suspicious_content"]],
    }


# --- gates and scoring --------------------------------------------------------

def read_fact(sheet, field_id, cfg):
    """Return (value, state); state is stated, unconfirmed, contradictory, unknown or not_offered."""
    fact = sheet["fields"][field_id]
    if fact["status"] == "not_found":
        return None, cfg["fields"][field_id]["absent_means"]
    return fact["value"], fact["status"]


def evaluate_gate(gate, sheet, cfg):
    outcomes, details = [], []
    for check in gate["checks"]:
        value, state = read_fact(sheet, check["field"], cfg)
        if state != "stated":
            # Gates act only on firm statements; anything else needs a person.
            outcomes.append("unresolved")
            details.append(f"{check['field']} is {state.replace('_', ' ')}")
            continue
        test, limit = check["test"], check.get("value")
        if test == "equals":
            ok = value == limit
        elif test == "is_true":
            ok = value is True
        elif test == "at_least":
            ok = _frac(value) >= _frac(limit)
        else:
            ok = _frac(value) <= _frac(limit)
        outcomes.append("pass" if ok else "fail")
        if not ok:
            details.append(f"{check['field']} = {value!r} fails {test}" + (f" {limit!r}" if "value" in check else ""))
    outcome = "fail" if "fail" in outcomes else "unresolved" if "unresolved" in outcomes else "pass"
    return {"id": gate["id"], "label": gate["label"], "outcome": outcome, "detail": "; ".join(details)}


def linear(value, zero_at, full_at):
    """0 at zero_at, 100 at full_at, straight line between, clamped to 0-100."""
    score = (_frac(value) - _frac(zero_at)) / (_frac(full_at) - _frac(zero_at)) * 100
    return min(max(score, Fraction(0)), Fraction(100))


def window_start(evaluation_date, years):
    d = date.fromisoformat(evaluation_date)
    try:
        return d.replace(year=d.year - int(years))
    except ValueError:  # 29 February
        return d.replace(year=d.year - int(years), day=28)


def period_inside_window(period, cfg, years):
    """True if every project in the stated period falls inside the window; None if that is not certain."""
    if period["kind"] == "last_n_years":
        return True if period["years"] <= years else None
    return True if date(period["year"], 1, 1) >= window_start(cfg["evaluation_date"], years) else None


def score_criterion(crit, sheet, cfg):
    """Return the 0-100 score (a Fraction) or None when an input is missing, plus notes."""
    result = {"id": crit["id"], "label": crit["label"], "weight": crit["weight"],
              "score": None, "inputs": "", "warnings": [], "missing": []}
    method = crit["method"]

    def numeric(field, zero_at, full_at):
        value, state = read_fact(sheet, field, cfg)
        if state in ("unknown", "contradictory"):
            result["missing"].append(f"{field} is {state}")
            return None, value
        if state == "not_offered":
            return Fraction(0), value
        if state == "unconfirmed":
            result["warnings"].append(f"{field} unconfirmed: {sheet['fields'][field].get('note') or 'needs confirmation'}")
        return linear(value, zero_at, full_at), value

    if method == "linear":
        result["score"], value = numeric(crit["field"], crit["zero_at"], crit["full_at"])
        result["inputs"] = f"{crit['field']}={value}"
    elif method == "composite":
        total, parts = Fraction(0), []
        for part in crit["parts"]:
            score, value = numeric(part["field"], part["zero_at"], part["full_at"])
            parts.append(f"{part['field']}={value}")
            if score is not None:
                total += score * _frac(part["share"]) / 100
        result["inputs"] = ", ".join(parts)
        result["score"] = None if result["missing"] else total
    elif method == "checklist":
        total, met = Fraction(0), []
        for item in crit["items"]:
            value, state = read_fact(sheet, item["field"], cfg)
            if state in ("unknown", "contradictory"):
                result["missing"].append(f"{item['field']} is {state}")
            elif state != "not_offered" and value not in (False, 0, None):
                total += _frac(item["points"])
                met.append(item["field"])
                if state == "unconfirmed":
                    result["warnings"].append(f"{item['field']} unconfirmed: {sheet['fields'][item['field']].get('note') or 'needs confirmation'}")
        result["inputs"] = "met: " + (", ".join(met) or "none")
        result["score"] = None if result["missing"] else total
    elif method == "count_in_window":
        score, count = numeric(crit["count_field"], crit["zero_at"], crit["full_at"])
        period, state = read_fact(sheet, crit["period_field"], cfg)
        inside = period_inside_window(period, cfg, crit["window_years"]) if state == "stated" else None
        if state != "stated":
            described = f"period {state.replace('_', ' ')}"
        elif period["kind"] == "since_year":
            described = f"since {period['year']}"
        else:
            described = f"in the last {period['years']:g} years"
        result["inputs"] = f"{count} projects {described}"
        if score is not None and inside is None:
            start = window_start(cfg["evaluation_date"], crit["window_years"])
            why = ("stated period may include projects before" if state == "stated"
                   else f"project period is {state.replace('_', ' ')}, so cannot confirm projects are after")
            note = f"project dates incomplete: {why} {start}; count used as stated, dated project list requested"
            if cfg["policies"]["incomplete_project_dates"] == "flag":
                result["missing"].append(note)
                score = None
            else:
                result["warnings"].append(note)
        result["score"] = score
    return result


def evaluate(sheet, cfg):
    """Gates first, then weighted scoring. Returns the full, explainable result."""
    validate_facts(sheet, cfg)
    gates = [evaluate_gate(g, sheet, cfg) for g in cfg["mandatory"] + cfg["feasibility"]]
    criteria = [score_criterion(c, sheet, cfg) for c in cfg["weighted"]]
    warnings = [w for c in criteria for w in c["warnings"]]
    hold = []
    for event in sheet.get("security_events", []):
        if cfg["policies"]["security_event"] == "hold":
            warnings.append(f"security: {event['summary']} Ignored as an instruction and logged; "
                            f"held as flagged until a reviewer clears it.")
            hold.append(f"security review pending ({event['location']})")
        else:
            warnings.append(f"security: {event['summary']} Ignored as an instruction and logged; "
                            f"reviewer must acknowledge before the run is finalised.")

    result = {"proposal": sheet["proposal"], "gates": gates, "criteria": criteria,
              "warnings": warnings, "total": None, "range_if_resolved": None, "rank": None}
    failed = [g for g in gates if g["outcome"] == "fail"]
    unresolved = [g for g in gates if g["outcome"] == "unresolved"]
    missing = [c for c in criteria if c["score"] is None]
    if failed:
        result["status"] = "ineligible"
        result["blockers"] = [f"{g['id']} {g['label']}: {g['detail']}" for g in failed]
    elif unresolved or missing or hold:
        result["status"] = "flagged"
        result["blockers"] = ([f"{g['id']} {g['label']}: {g['detail']}" for g in unresolved]
                              + [f"{c['label']}: {'; '.join(c['missing'])}" for c in missing] + hold)
        known = sum((c["score"] * _frac(c["weight"]) / 100 for c in criteria if c["score"] is not None), Fraction(0))
        open_weight = sum((_frac(c["weight"]) for c in missing), Fraction(0))
        result["range_if_resolved"] = (known, known + open_weight)
    else:
        result["status"] = "ranked"
        result["blockers"] = []
        result["total"] = sum((c["score"] * _frac(c["weight"]) / 100 for c in criteria), Fraction(0))
    return result


def to_decimal(value, cfg):
    places = Decimal(1).scaleb(-cfg["policies"]["decimal_places"])
    return (Decimal(value.numerator) / Decimal(value.denominator)).quantize(places, rounding=ROUND_HALF_UP)


def rank(results, cfg):
    """Rank by rounded total, then by criterion scores in descending weight order.

    A tie that survives every tie-break shares the rank and is left to the reviewer.
    """
    order = sorted(range(len(cfg["weighted"])), key=lambda i: -_frac(cfg["weighted"][i]["weight"]))

    def key(r):
        return (to_decimal(r["total"], cfg),) + tuple(to_decimal(r["criteria"][i]["score"], cfg) for i in order)

    ranked = sorted((r for r in results if r["status"] == "ranked"), key=lambda r: r["proposal"])
    ranked.sort(key=key, reverse=True)
    previous = None
    for position, r in enumerate(ranked, start=1):
        if previous is not None and key(r) == key(previous):
            r["rank"] = previous["rank"]
            for tied in (r, previous):
                if "tie: reviewer decides" not in tied["warnings"]:
                    tied["warnings"].append("tie: reviewer decides")
        else:
            r["rank"] = position
        previous = r
    others = sorted((r for r in results if r["status"] != "ranked"),
                    key=lambda r: (r["status"] != "flagged", r["proposal"]))
    return ranked + others


def run(cfg, facts):
    validate_config(cfg)
    if facts.get("criteria_version") != cfg["criteria_version"]:
        raise FactsError(f"facts were approved under {facts.get('criteria_version')!r}, "
                         f"snapshot is {cfg['criteria_version']!r}; versions are never mixed")
    return rank([evaluate(sheet, cfg) for sheet in facts["proposals"]], cfg)


# --- output -------------------------------------------------------------------

def _fmt(value, cfg):
    return f"{to_decimal(value, cfg)}"


def machine_ranking(results, cfg):
    """Same shape as ranking.json; reasons here are generated, the repo file's are written for people."""
    rows = []
    for r in results:
        if r["status"] == "ranked":
            reason = "Passes all gates; " + ", ".join(
                f"{c['id']} {_fmt(c['score'], cfg)}" for c in r["criteria"])
            if r["warnings"]:
                reason += f"; {len(r['warnings'])} warning(s) for the reviewer"
        else:
            reason = "; ".join(r["blockers"])
            if r["range_if_resolved"]:
                low, high = r["range_if_resolved"]
                if low == high:
                    reason += f"; score if resolved {_fmt(low, cfg)}"
                else:
                    reason += f"; score range if resolved {_fmt(low, cfg)}-{_fmt(high, cfg)}"
        rows.append({"proposal": r["proposal"], "rank": r["rank"], "status": r["status"],
                     "score": float(to_decimal(r["total"], cfg)) if r["total"] is not None else None,
                     "reason": reason})
    return rows


def print_breakdown(results, cfg):
    print(f"Criteria snapshot {cfg['criteria_version']} | evaluation date {cfg['evaluation_date']} | "
          f"weights total {sum(_frac(c['weight']) for c in cfg['weighted'])}%")
    for r in results:
        head = f"#{r['rank']}  score {_fmt(r['total'], cfg)}" if r["status"] == "ranked" else "score null"
        print(f"\n{r['proposal']}  {r['status'].upper()}  {head}")
        print("  gates: " + " | ".join(f"{g['id']} {g['outcome']}" for g in r["gates"]))
        if r["status"] != "ineligible":
            for c in r["criteria"]:
                if c["score"] is None:
                    print(f"  {c['label']:<20} {c['weight']:>3}%  missing  ({'; '.join(c['missing'])})")
                else:
                    contribution = c["score"] * _frac(c["weight"]) / 100
                    print(f"  {c['label']:<20} {c['weight']:>3}%  score {_fmt(c['score'], cfg):>6}  "
                          f"contribution {_fmt(contribution, cfg):>5}  ({c['inputs']})")
        for b in r["blockers"]:
            print(f"  BLOCKER: {b}")
        if r["range_if_resolved"]:
            low, high = r["range_if_resolved"]
            if low == high:
                print(f"  score if resolved: {_fmt(low, cfg)}")
            else:
                print(f"  score range if resolved: {_fmt(low, cfg)} to {_fmt(high, cfg)}")
        for w in r["warnings"]:
            print(f"  warning: {w}")


def check_file(results, cfg, path):
    """Compare proposal, rank, status and score with a ranking file (reasons are prose)."""
    expected = {row["proposal"]: row for row in machine_ranking(results, cfg)}
    actual = json.loads(Path(path).read_text(encoding="utf-8"))
    problems = []
    letters = [row.get("proposal") for row in actual]
    if sorted(letters) != sorted(expected) or len(set(letters)) != len(letters):
        problems.append(f"proposals in file {letters} != {sorted(expected)}")
    for row in actual:
        want = expected.get(row.get("proposal"))
        if want is None:
            continue
        if list(row) != ["proposal", "rank", "status", "score", "reason"]:
            problems.append(f"{row['proposal']}: fields must be proposal, rank, status, score, reason")
        for key in ("rank", "status", "score"):
            if row.get(key) != want[key]:
                problems.append(f"{row['proposal']}.{key}: file has {row.get(key)!r}, rubric gives {want[key]!r}")
        if not isinstance(row.get("reason"), str) or not row["reason"].strip():
            problems.append(f"{row['proposal']}: reason must be a non-empty string")
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--config", default=HERE / "criteria_snapshot_v1.json")
    parser.add_argument("--facts", default=HERE / "approved_facts.json")
    parser.add_argument("--json", action="store_true", help="print the machine ranking as JSON")
    parser.add_argument("--check", metavar="RANKING_JSON", help="compare a ranking file with the rubric")
    parser.add_argument("--prompt-fields", action="store_true",
                        help="print the {{fields_json}} block the extraction prompt is given")
    args = parser.parse_args(argv)
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    if args.prompt_fields:
        validate_config(cfg)
        print(json.dumps(prompt_fields(cfg), indent=2))
        return 0
    facts = json.loads(Path(args.facts).read_text(encoding="utf-8"))
    results = run(cfg, facts)
    if args.check:
        problems = check_file(results, cfg, args.check)
        for p in problems:
            print("MISMATCH:", p)
        print("ranking file matches the rubric" if not problems else f"{len(problems)} mismatch(es)")
        return 1 if problems else 0
    if args.json:
        print(json.dumps(machine_ranking(results, cfg), indent=2))
    else:
        print_breakdown(results, cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
