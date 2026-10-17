"""The question shapes supported by the operations that answer them."""

QUESTION_CHOICES = {
    "conflict": (2, {"use_new", "keep_mine", "keep_both"}),
    "confirm_rule": (1, {"make_rule", "keep_note", "forget"}),
    "still_true": (1, {"keep", "forget"}),
}
_MISSING = object()


def check_question(kind, *, record_ids=_MISSING, options=_MISSING) -> None:
    """Validate known fields; callers merge partial updates with stored fields."""
    if kind not in QUESTION_CHOICES:
        raise ValueError("unsupported question kind")
    count, choices = QUESTION_CHOICES[kind]
    if record_ids is not _MISSING and (
        not isinstance(record_ids, (list, tuple)) or len(record_ids) != count
        or any(not isinstance(value, str) or not value.strip() for value in record_ids)
        or len(set(record_ids)) != count
    ):
        raise ValueError(f"{kind} questions require {count} distinct memory ids")
    if options is not _MISSING:
        if not isinstance(options, (list, tuple)) or any(
            not isinstance(option, dict) or not isinstance(option.get("key"), str)
            or not isinstance(option.get("label"), str) or not option["label"].strip() for option in options
        ):
            raise ValueError("invalid question options")
        keys = [option["key"] for option in options]
        if len(keys) != len(choices) or set(keys) != choices:
            raise ValueError(f"unsupported or missing choices for {kind} questions")
