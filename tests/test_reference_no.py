import re
from datetime import datetime


def test_make_reference_no_format(app_module):
    ref = app_module.make_reference_no("WIKA", 0)
    assert re.fullmatch(r"WIKA-\d{6}#000", ref)


def test_make_reference_no_strips_punctuation_and_takes_first_token(app_module):
    ref = app_module.make_reference_no("Honeywell CCC / IMI Remosa / WIKA", 5)
    assert ref.startswith("HONEYWELLCCC-")
    assert ref.endswith("#005")


def test_make_reference_no_falls_back_when_brand_blank(app_module):
    assert app_module.make_reference_no("", 0).startswith("INQ-")
    assert app_module.make_reference_no("   ", 0).startswith("INQ-")


def test_next_reference_sequence_increments(app_module):
    first = app_module.next_reference_sequence()
    second = app_module.next_reference_sequence()
    third = app_module.next_reference_sequence()
    assert (first, second, third) == (0, 1, 2)


def test_next_reference_sequence_wraps_after_999(app_module):
    year = datetime.now(app_module.ZoneInfo("Asia/Jakarta")).year
    # Pre-seed the fake counter document so we don't loop 1000 times to get there.
    app_module.counters.docs.append({"_id": f"inquiry_reference_{year}", "seq": 999})
    last_before_wrap = app_module.next_reference_sequence()
    assert last_before_wrap == 999
    first_after_wrap = app_module.next_reference_sequence()
    assert first_after_wrap == 0  # rolls back to 000
