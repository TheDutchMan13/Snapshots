"""similar_pairs finds near-identical fingerprints. The numpy version must give exactly the pairs of the
plain version, for big buckets and small ones."""

import random
from collections import defaultdict

from snapshots import analysis


def naive(items, distance):
    found = set()
    unsigned = {i: analysis._unsigned(h) for i, h in items}
    for lo, hi in analysis._bands(distance):
        mask = ((1 << (hi - lo)) - 1) << lo
        buckets = defaultdict(list)
        for i, h in unsigned.items():
            buckets[h & mask].append(i)
        for ids in buckets.values():
            if len(ids) < 2 or len(ids) > 2000:
                continue
            for x in range(len(ids)):
                for y in range(x + 1, len(ids)):
                    if (unsigned[ids[x]] ^ unsigned[ids[y]]).bit_count() <= distance:
                        a, b = ids[x], ids[y]
                        found.add((a, b) if a < b else (b, a))
    return found


def hashes(rng, n, groups):
    """n signed 64-bit fingerprints, many of them a few bits away from each other (so buckets are crowded)."""
    base = [rng.getrandbits(64) for _ in range(groups)]
    out = []
    for i in range(n):
        h = rng.choice(base)
        for _ in range(rng.randint(0, 12)):
            h ^= 1 << rng.randrange(64)
        out.append((i + 1, h - (1 << 64) if h >= (1 << 63) else h))     # as stored: signed
    return out


def test_the_same_pairs_as_the_plain_version():
    for seed, n, groups, distance in [(1, 300, 4, 8), (2, 1500, 3, 6), (3, 700, 40, 10), (4, 90, 2, 8), (5, 40, 1, 5)]:
        items = hashes(random.Random(seed), n, groups)
        assert analysis.similar_pairs(items, distance) == naive(items, distance), (seed, n)


def test_small_inputs():
    assert analysis.similar_pairs([], 8) == set()
    assert analysis.similar_pairs([(1, 5)], 8) == set()
    assert analysis.similar_pairs([(1, 5), (2, 5), (3, -1)], 8) == {(1, 2)}
    big = [(i, 0) for i in range(1, 60)] + [(100, -1)]       # 59 identical fingerprints and one very different
    assert len(analysis.similar_pairs(big, 8)) == 59 * 58 // 2


def test_a_giant_crowd_is_skipped_as_before():
    items = [(i, 0) for i in range(1, 2002)]                 # 2001 identical: a meaningless pattern
    assert analysis.similar_pairs(items, 8) == set() == naive(items, 8)


def test_the_table_fallback_for_older_numpy_gives_the_same_pairs(monkeypatch):
    monkeypatch.setattr(analysis, "_bitwise_count", None)
    items = hashes(random.Random(7), 800, 5)
    assert analysis.similar_pairs(items, 8) == naive(items, 8)
