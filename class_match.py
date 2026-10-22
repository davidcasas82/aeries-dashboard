"""Matching a ClassSummary row to its GradebookDetails assignments. Stdlib only.

The scrape and the Mac mini runner both need it, and the runner's SSD venv has no requests or
bs4, so it lives here rather than in scraper.py. scraper.py imports it; assignment_group_for_class
there uses match_class_groups, so both paths share one rule.
"""

import re


def extract_period_from_label(label):
    m = re.search(r"<<\s*(\d+)-", label or "")
    if m:
        return int(m.group(1))
    m2 = re.match(r"^(\d+)-", label or "")
    return int(m2.group(1)) if m2 else None


def _norm_course_name(name):
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _period_value(value):
    """An int period, or None for a missing, empty, whitespace-only or non-numeric one."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _group_period(group):
    period = _period_value(group.get("period"))
    if period is None:
        period = extract_period_from_label(group.get("class_name") or "")
    return period


def _names_overlap(a, b):
    return bool(a and b and (a in b or b in a))


def _group_key(group):
    return (_group_period(group), _norm_course_name(group.get("class_name")) or (group.get("class_name") or ""))


def match_class_groups(groups, class_meta, classes=None):
    """The GradebookDetails groups for one ClassSummary row, and same-name groups lost to a period.

    - When the class row and a group both have a period, the period decides: same period
      matches, a different period never does, whatever the names say.
    - Otherwise the exact normalized name wins. Only when no group matches exactly does a
      contains match count, and only when it is unambiguous: exactly one group contains
      (or is contained in) the class name, and no other row in ``classes`` claims that group
      by period, exact name or its own contains match. An ambiguous name lands nowhere.

    Returns (matched groups, deduped in order; periods of exact-name groups skipped because
    their period differs). So "Math" in period 3 never picks up "AP Math Analysis" in
    period 5, and without periods "Math" only takes the "Math" group.
    """
    period = _period_value((class_meta or {}).get("period"))
    course_n = _norm_course_name((class_meta or {}).get("course_name"))
    hits = []
    name_pool = []
    other_period = []
    for group in groups or []:
        group_period = _group_period(group)
        if period is not None and group_period is not None:
            if group_period == period:
                hits.append(group)
            elif course_n and _norm_course_name(group.get("class_name")) == course_n:
                other_period.append(group_period)
            continue
        name_pool.append(group)

    if course_n and name_pool:
        exact = [g for g in name_pool if _norm_course_name(g.get("class_name")) == course_n]
        if exact:
            hits.extend(exact)
        else:
            loose = [g for g in name_pool if _names_overlap(course_n, _norm_course_name(g.get("class_name")))]
            if len({_group_key(g) for g in loose}) == 1 and not _group_claimed_elsewhere(classes, class_meta, loose[0]):
                hits.extend(loose)

    matched = []
    seen = set()
    for group in hits:
        key = _group_key(group)
        if key in seen:
            continue
        seen.add(key)
        matched.append(group)
    return matched, sorted(set(other_period))


def _group_claimed_elsewhere(classes, class_meta, group):
    """True when another class row could also take this group (by period, exact name or contains)."""
    name_n = _norm_course_name(group.get("class_name"))
    group_period = _group_period(group)
    own = (_period_value((class_meta or {}).get("period")), _norm_course_name((class_meta or {}).get("course_name")))
    for other in classes or []:
        other_n = _norm_course_name(other.get("course_name"))
        other_period = _period_value(other.get("period"))
        if not other_n or (other_period, other_n) == own:
            continue
        if other_period is not None and group_period is not None:
            if other_period == group_period:
                return True  # that class owns the group by period
            continue
        if _names_overlap(other_n, name_n):
            return True
    return False


def _has_gradebook_group(groups, class_meta):
    """Some group belongs to this row by period or plausibly by name (exact or contains)."""
    period = _period_value((class_meta or {}).get("period"))
    course_n = _norm_course_name((class_meta or {}).get("course_name"))
    for group in groups or []:
        group_period = _group_period(group)
        if period is not None and group_period == period:
            return True
        if _names_overlap(course_n, _norm_course_name(group.get("class_name"))):
            return True
    return False


# One warning per class per run: assignments_for_class runs many times per scrape.
_MATCH_WARNED = set()


def _warn_match_once(student_data, class_meta, kind, message):
    key = (
        str((student_data or {}).get("sn")),
        kind,
        _period_value((class_meta or {}).get("period")),
        _norm_course_name((class_meta or {}).get("course_name")),
    )
    if key in _MATCH_WARNED:
        return
    _MATCH_WARNED.add(key)
    print(f"::warning::class match: {message}")


def assignments_for_class(student_data, class_meta):
    """Every assignment in the GradebookDetails groups matching a ClassSummary row.

    The rule is match_class_groups. No silent zeros: at most one warning per class per run
    (name and periods only, never scores). A same-name group in a different period gets
    the period warning; otherwise a class that has a gradebook group but matched none gets
    the no-match warning. A matched group that is simply empty (early in a term) is quiet.
    """
    groups = student_data.get("assignments_by_class") or []
    matched, other_periods = match_class_groups(groups, class_meta, student_data.get("classes"))
    assignments = []
    for group in matched:
        assignments.extend(group.get("assignments") or [])
    name = ((class_meta or {}).get("course_name") or "").strip()
    if name:
        period = _period_value(class_meta.get("period"))
        if other_periods:
            _warn_match_once(
                student_data, class_meta, "other_period",
                f"{name!r} is period {period} but its same-name gradebook group is period "
                f"{', '.join(str(p) for p in other_periods)}; not matched",
            )
        elif not matched and _has_gradebook_group(groups, class_meta):
            _warn_match_once(
                student_data, class_meta, "zero",
                f"{name!r} (period {period}) has a gradebook group but matched none",
            )
    return assignments
