"""Publication timestamps used to reuse completed coverage of ordered sources."""


def publication_time(value):
    if type(value) is int:
        return value if 0 < value < 2**63 else None
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 18:
        return int(value) or None
    return None


def ordered_page_times(timestamps, now, previous_oldest=None):
    """Return (newest, oldest) only for a fully dated, descending page."""
    if not timestamps or any(not stamp or stamp > now for stamp in timestamps):
        return None
    if previous_oldest is not None and timestamps[0] > previous_oldest:
        return None
    if any(newer < older for newer, older in zip(timestamps, timestamps[1:])):
        return None
    return timestamps[0], timestamps[-1]


def search_page_exhausted(data, page):
    page_count = data.get("numPages")
    if type(page_count) is int and page_count > 0:
        return page >= page_count
    result_count = data.get("numResults")
    return data.get("result") == [] or (type(result_count) is int and result_count == 0 and not data.get("result"))
