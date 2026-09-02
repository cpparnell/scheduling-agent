import re

# A month name, a numeric date (N/N or N/N/N), or an ordinal day reference
# ("the 14th") — the class of message content that pins a plan to a specific
# calendar date rather than a bare weekday name. Shared by:
# - detector._reconcile_weekday: an explicit date already in the model's own
#   evidence should never be overridden by weekday-shift heuristics.
# - reader's anchor harvesting (the base signal for "this message anchors a
#   date", extended below with holidays and "week of" phrasing).
_MONTH_NUMERIC_ORDINAL = (
    r"(?i:\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b)"
    r"|\bMay\b"                                    # only capitalized — 'you may want' is not a date
    r"|\b\d{1,2}[/.-]\d{1,2}(?:[/.-]\d{2,4})?\b"    # 10/14, 10-14, 10/14/26
    r"|\b\d{1,2}(?:st|nd|rd|th)\b"                  # the 14th
)

# Holiday names and "week(end) of" phrasing people use to anchor a plan
# without any numeric date at all ("cabin trip is Labor Day weekend!").
# Broader than the explicit-date signal above; used only for anchor-message
# harvesting (reader.py), not the stricter _reconcile_weekday override check.
_HOLIDAYS_AND_WEEK_OF = (
    r"(?i:\bnew year'?s\b|\bmlk\b|\bpresidents?\s+day\b|\bmemorial\s+day\b|"
    r"\bjuneteenth\b|\bfourth\s+of\s+july\b|\bjuly\s+4(?:th)?\b|\blabor\s+day\b|"
    r"\bthanksgiving\b|\bchristmas\b|\bnye\b|\bweek(?:end)?\s+of\b)"
)

EXPLICIT_DATE_RE = re.compile(_MONTH_NUMERIC_ORDINAL)
ANCHOR_PATTERN = re.compile(f"(?:{_MONTH_NUMERIC_ORDINAL})|(?:{_HOLIDAYS_AND_WEEK_OF})")
