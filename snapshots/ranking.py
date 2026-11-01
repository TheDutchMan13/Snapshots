"""Putting two lists of search results together: "Best matches".

A search is answered two ways: by words (names, places, tags, text in the photo, dates), and by what the
photo shows (search by content). Reciprocal rank fusion merges the two lists: each photo scores
1 / (K + its place in a list) for every list it is in, so a photo found both ways comes first, and
the best of each list come next, taking turns. K = 60 is the usual value (Cormack, Clarke and
Büttcher, 2009); it keeps one list's first few places from outweighing everything else.

Words results have no order of their own besides date, so they are ranked by how many of the words
were found in strong places (a person's name, a tag, the description, the place, a landmark, the
caption) before weak ones
(the file name, the camera, text read in the photo), then newest first.
"""

K = 60


def fuse(*lists: list[int], k: int = K, weights: tuple[float, ...] | None = None) -> list[tuple[int, float]]:
    """[(id, score)] best first, from lists of ids that are each best first."""
    weights = weights or (1.0,) * len(lists)
    score: dict[int, float] = {}
    first_seen: dict[int, int] = {}
    order = 0
    for lst, w in zip(lists, weights):
        for rank, ident in enumerate(lst):
            score[ident] = score.get(ident, 0.0) + w / (k + rank + 1)
            if ident not in first_seen:
                first_seen[ident] = order
                order += 1
    return sorted(score.items(), key=lambda x: (-x[1], first_seen[x[0]]))


STRONG_FIELDS = ("people", "tags", "description", "city", "state", "country", "landmark", "caption")


def word_strength(row: dict, tokens: list[str], weights: dict[str, float] | None = None) -> float:
    """How strongly this row matches: each word found in a strong place (see STRONG_FIELDS) counts its
    weight (its rarity, see spelling.py; 1 without weights), a word found only elsewhere counts a fifth."""
    from .spelling import related

    total = 0.0
    for t in tokens:
        w = (weights or {}).get(t, 1.0)
        strong = False
        for word in related(t):
            for field in STRONG_FIELDS:
                value = row.get(field)
                if isinstance(value, (list, tuple)):
                    strong = any(word in (v or "").lower() for v in value)
                else:
                    strong = bool(value) and word in str(value).lower()
                if strong:
                    break
            if strong:
                break
        total += w if strong else 0.2 * w
    return total
