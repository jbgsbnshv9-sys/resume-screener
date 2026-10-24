from screener.pipeline.spans import QuoteLocator, normalize


def test_exact_match():
    loc = QuoteLocator("Built REST APIs in Go.\nLed migrations.")
    span = loc.find("Led migrations.")
    assert span is not None and span.slice(loc.source) == "Led migrations."


def test_whitespace_and_typography_normalized():
    src = "Owned the “order” service —\nend to end"
    loc = QuoteLocator(src)
    span = loc.find('Owned the "order" service - end to end')
    assert span is not None
    assert span.slice(src) == src


def test_missing_quote_does_not_resolve():
    loc = QuoteLocator("Built REST APIs in Go.")
    assert loc.find("Built REST APIs in Rust.") is None
    assert loc.find("") is None
    assert loc.find("   ") is None


def test_start_hint_prefers_later_occurrence():
    src = "Python\nSkills: Python"
    span = QuoteLocator(src).find("Python", start_hint=3)
    assert span is not None and span.start == src.rindex("Python")


def test_normalize_collapses_runs():
    assert normalize("  a \n\n b\t c  ") == "a b c"
