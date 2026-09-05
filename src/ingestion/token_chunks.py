"""Original-text chunks bounded by the configured embedding tokenizer."""


def token_chunks(text, provider, *, max_tokens=None, overlap_tokens=32, max_chars=32768):
    limit = int(provider.token_limit())
    budget = limit if max_tokens is None else int(max_tokens)
    if not 1 <= budget <= limit or not 0 <= overlap_tokens < budget or max_chars < 1:
        raise ValueError("invalid token chunk limits")
    start = 0
    while start < len(text):
        lo, hi = start + 1, min(len(text), start + max_chars)
        end = start
        while lo <= hi:
            middle = (lo + hi) // 2
            if provider.count_tokens(text[start:middle]) <= budget:
                end, lo = middle, middle + 1
            else:
                hi = middle - 1
        if end == start:
            raise ValueError("a source character cannot fit in the tokenizer budget")
        # Prefer a complete whitespace boundary when that still advances.
        if end < len(text):
            boundary = max(text.rfind(" ", start + 1, end), text.rfind("\n", start + 1, end))
            if boundary > start:
                end = boundary + 1
        count = provider.count_tokens(text[start:end])
        if count > budget:
            raise ValueError("tokenizer produced an inconsistent chunk count")
        yield {"text": text[start:end], "start_offset": start, "end_offset": end, "token_count": count}
        if end == len(text):
            break
        if not overlap_tokens:
            start = end
            continue
        lo, hi, next_start = start + 1, end, end
        while lo <= hi:
            middle = (lo + hi) // 2
            if provider.count_tokens(text[middle:end]) <= overlap_tokens:
                next_start, hi = middle, middle - 1
            else:
                lo = middle + 1
        start = max(start + 1, next_start)
