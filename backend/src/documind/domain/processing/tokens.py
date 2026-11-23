"""Token estimation.

The exact tokenizer data can't be fetched in every environment, so chunk
sizing uses a deliberately conservative estimate (it over-counts for typical
English prose): max(chars / 4, words * 4 / 3). Chunks therefore stay within the
configured budget and far below embedding-model input limits.
"""

from __future__ import annotations

import math


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(math.ceil(len(text) / 4), math.ceil(len(text.split()) * 4 / 3))
