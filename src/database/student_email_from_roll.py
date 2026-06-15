"""Derive FAST NU student emails from roll numbers (seating plan upload, imports, etc.)."""

from __future__ import annotations

import re

NU_STUDENT_EMAIL_DOMAIN = "nu.edu.pk"


def generate_nu_student_email_from_roll(roll_number: str) -> str:
    """
    Build @nu.edu.pk address from roll number.

    Standard pattern ``XXY-AAAA`` → ``yxxaaaa@nu.edu.pk`` (program letter first, lowercase).

    Examples:
        - ``22I-0857`` → ``i220857@nu.edu.pk``
        - ``24I-2091`` → ``i242091@nu.edu.pk``
        - ``22P-0507`` → ``p220507@nu.edu.pk``

    Also accepts ``XXYAAAA`` (no hyphen) when the letter is unambiguous.
    """
    raw = (roll_number or "").strip().replace(" ", "")

    m = re.match(r"^(\d{2})([A-Za-z])-(\d+)$", raw)
    if m:
        digits1, letter, tail = m.group(1), m.group(2).lower(), m.group(3)
        return f"{letter}{digits1}{tail}@{NU_STUDENT_EMAIL_DOMAIN}"

    m2 = re.match(r"^(\d{2})([A-Za-z])(\d+)$", raw)
    if m2:
        digits1, letter, tail = m2.group(1), m2.group(2).lower(), m2.group(3)
        return f"{letter}{digits1}{tail}@{NU_STUDENT_EMAIL_DOMAIN}"

    raise ValueError(
        f"Cannot derive NU student email from roll number {roll_number!r}. "
        "Expected formats like 24I-2091, 22P-0507, or 24I2091."
    )


def legacy_bug_nu_student_email(roll_number: str) -> str:
    """
    Email shape from the old seating-plan bug: strip, lower, remove hyphens/spaces, + @nu.edu.pk.

    Example: ``24I-2102`` → ``24i2102@nu.edu.pk`` (wrong). Correct is ``i242102@nu.edu.pk``.
    """
    raw = (roll_number or "").strip().lower().replace("-", "").replace(" ", "")
    return f"{raw}@{NU_STUDENT_EMAIL_DOMAIN}"


def corrected_nu_email_if_legacy_bug(roll_number: str, current_email: str) -> str | None:
    """
    If ``current_email`` is exactly the legacy wrong address for this roll, return the correct one.

    Used when re-processing seating plans so existing rows are repaired without clobbering
    manually edited addresses.
    """
    try:
        correct = generate_nu_student_email_from_roll(roll_number)
    except ValueError:
        return None
    cur = (current_email or "").strip().lower()
    if cur == correct.lower():
        return None
    if cur == legacy_bug_nu_student_email(roll_number).lower():
        return correct
    return None
