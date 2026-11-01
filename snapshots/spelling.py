"""Forgiving word search: typos, related words, and rare words counting more.

* Typos: a search word that is in nothing in the library and is not an ordinary English word is
  replaced by the nearest word that is (at most one letter wrong in short words, two in longer ones;
  a swap of two letters counts as one). The library's own words (places, names, tags, descriptions,
  folder names, text read in photos) come first, because they find photos; the English words come from
  the search-by-content model's vocabulary when it is downloaded, so "sunest" still becomes "sunset" for
  a search by content. Candidates are found with a table of single-letter deletions (Garbe's SymSpell),
  so a typo is checked against tens of thousands of words in a few milliseconds.
* Related words: "pup" also finds "dog", "xmas" also finds "christmas" (RELATED).
* Rare words count more when ranking word results (inverse document frequency, Spärck Jones 1972):
  a photo that has the rare word of a search in a strong place comes before one that has the common one.
"""

import json
import logging
import math
import re
import threading
import time
from collections import Counter
from functools import lru_cache

log = logging.getLogger("snapshots.spelling")

WORD = re.compile(r"[^\W\d_]{3,}", re.UNICODE)
REFRESH_SECONDS = 300      # the library's words are gathered again at most this often
MAX_WORD = 24

# Groups of words that mean the same in a search (each finds the others). Kept short and plain.
# Groups of words that mean the same in a search (each finds the others). Words are matched anywhere in a
# name or text, so short words that hide inside others ("sea" in Seattle, "oma" in tomato) are left out.
RELATED = [
    ("gray", "grey"), ("dog", "puppy", "doggy"), ("cat", "kitten", "kitty"), ("christmas", "xmas"),
    ("birthday", "bday"), ("beach", "seaside"), ("mom", "mommy", "mother"), ("dad", "daddy", "father"),
    ("grandma", "granny", "grandmother"), ("grandpa", "grandfather"), ("baby", "infant", "newborn"),
    ("child", "children"), ("bike", "bicycle"), ("mountain", "mountains"), ("party", "celebration"),
    ("wedding", "bride"), ("sunset", "sundown"), ("snow", "snowy"), ("vacation", "holiday"),
]
_RELATED = {w: g for g in RELATED for w in g}


def related(token: str) -> tuple[str, ...]:
    """The token and the words that mean the same."""
    return _RELATED.get(token, (token,))


# ---------------------------------------------------------------- edit distance

def distance(a: str, b: str, limit: int = 2) -> int:
    """Optimal string alignment distance (insert, delete, change, or swap two neighbours), up to limit+1."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev2, prev = None, list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        low = cur[0]
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
            low = min(low, cur[j])
        if low > limit:
            return limit + 1
        prev2, prev = prev, cur
    return prev[-1]


def _deletes(word: str, depth: int) -> set[str]:
    out, edge = set(), {word}
    for _ in range(depth):
        nxt = set()
        for w in edge:
            for i in range(len(w)):
                nxt.add(w[:i] + w[i + 1:])
        out |= nxt
        edge = nxt
    return out


class WordTable:
    """Words with how often each occurs, and a deletion table to find near words quickly."""

    def __init__(self, counts: dict[str, int]):
        self.counts = counts
        self.words = list(counts)
        self.near: dict[str, list[str]] = {}
        for w in self.words:
            for d in _deletes(w, 1):
                self.near.setdefault(d, []).append(w)

    def candidates(self, token: str, limit: int) -> list[tuple[int, str]]:
        """[(distance, word)] for words within `limit` of token."""
        found = set()
        probes = {token} | _deletes(token, limit)
        if limit >= 2:      # two letters swapped and one more mistake: reach it through the swap
            for i in range(len(token) - 1):
                swapped = token[:i] + token[i + 1] + token[i] + token[i + 2:]
                probes |= {swapped} | _deletes(swapped, 1)
        for p in probes:
            if p in self.counts:
                found.add(p)
            found.update(self.near.get(p, ()))
        out = []
        for w in found:
            d = distance(token, w, limit)
            if d <= limit:
                out.append((d, w))
        return out


def allowed_typos(token: str) -> int:
    return 0 if len(token) < 4 else 1 if len(token) < 7 else 2


# ---------------------------------------------------------------- the library's words

def library_words(conn) -> Counter:
    """How many photos each word is in (places, descriptions, tags, names, folder names, text read in photos).
    Words read in only one photo are left out: text recognition makes up a few."""
    df: Counter = Counter()
    ocr: Counter = Counter()
    for row in conn.execute("SELECT path, city, state, country, description, ocr_text, colors, camera, caption,"
                            " landmark FROM assets WHERE trashed_at IS NULL AND locked_by IS NULL"):
        words = set()
        for text in (row[0].replace("\\", "/"), row[1], row[2], row[3], row[4], row[6], row[7], row[8], row[9]):
            if text:
                words.update(w for w in WORD.findall(text.lower()) if len(w) <= MAX_WORD)
        df.update(words)
        if row[5]:
            ocr.update({w for w in WORD.findall(row[5].lower()) if len(w) <= MAX_WORD} - words)
    df.update({w: n for w, n in ocr.items() if n >= 2})
    for name, n in conn.execute(
            "SELECT t.name, COUNT(*) FROM tags t JOIN asset_tags at ON at.tag_id = t.id"
            " JOIN assets a ON a.id = at.asset_id AND a.trashed_at IS NULL AND a.locked_by IS NULL GROUP BY t.id"):
        for w in set(WORD.findall((name or "").lower())):
            df[w] += n
    for name, n in list(conn.execute(
            "SELECT p.name, COUNT(DISTINCT f.asset_id) FROM people p JOIN faces f ON f.person_id = p.id"
            " JOIN assets a ON a.id = f.asset_id AND a.trashed_at IS NULL AND a.locked_by IS NULL"
            " WHERE p.name IS NOT NULL AND p.hidden = 0 GROUP BY p.id")) + list(conn.execute(
            "SELECT p.name, COUNT(DISTINCT s.asset_id) FROM pets p JOIN pet_sightings s ON s.pet_id = p.id"
            " JOIN assets a ON a.id = s.asset_id AND a.trashed_at IS NULL AND a.locked_by IS NULL"
            " WHERE p.name IS NOT NULL AND p.hidden = 0 GROUP BY p.id")):
        for w in set(WORD.findall(name.lower())):
            df[w] += n
    return df


def english_words(vocab_path) -> set[str]:
    """Whole words of the content-search model's vocabulary (about 30,000 common English words)."""
    try:
        with open(vocab_path, encoding="utf-8") as fh:
            vocab = json.load(fh)
    except (OSError, ValueError):
        return set()
    return {k[:-4] for k in vocab if k.endswith("</w>") and k[:-4].isalpha() and 3 <= len(k) - 4 <= MAX_WORD}


class Speller:
    """Corrects search words, from the library's words and (when there) the English words."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._built_at = -1e9
        self._key = None
        self.library: WordTable | None = None
        self.english: WordTable | None = None
        self.total = 0
        self._building = False
        self._checked: dict[str, tuple[float, bool]] = {}   # q -> (when, finds something as typed)

    def _english_path(self):
        return self.cfg.model_dir / "clip" / "clip_vocab.json"

    def refresh(self, conn, now: float | None = None) -> None:
        """Gather the words again if anything changed (slow on a big library: run in the background)."""
        now = time.monotonic() if now is None else now
        key = conn.execute("SELECT COUNT(*), COALESCE(MAX(id), 0), COALESCE(SUM(length(coalesce(description, ''))"
                           " + length(coalesce(ocr_text, '')) + length(coalesce(city, ''))"
                           " + length(coalesce(caption, '')) + length(coalesce(landmark, ''))), 0),"
                           " COUNT(locked_by), COUNT(colors) FROM assets WHERE trashed_at IS NULL").fetchone()
        key = tuple(key) + tuple(conn.execute(
            "SELECT (SELECT COUNT(*) FROM asset_tags), (SELECT COUNT(*) FROM people WHERE name IS NOT NULL),"
            " (SELECT COUNT(*) FROM pets WHERE name IS NOT NULL)").fetchone())
        english = self.english
        if english is None and self._english_path().is_file():
            started = time.monotonic()
            english = WordTable({w: 1 for w in english_words(self._english_path())})
            log.info("English words ready (%d) in %.1fs", len(english.words), time.monotonic() - started)
        library = self.library
        if key != self._key or library is None:
            library = WordTable(dict(library_words(conn)))
        with self._lock:                     # swap in the new tables at once; searches never wait on a build
            changed = library is not self.library or english is not self.english
            self.library, self.english = library, english
            self.total = int(key[0])
            self._key = key
            self._built_at = now
            if changed:
                _correct.cache_clear()
                self._checked.clear()

    def ensure(self) -> None:
        """Start gathering the words in the background if they are missing or due. Returns at once: until
        they are ready, searches are left as typed."""
        with self._lock:
            if self._building or (self.library is not None and time.monotonic() - self._built_at < REFRESH_SECONDS):
                return
            self._building = True

        def build():
            from . import db

            try:
                with db.session(self.cfg.db_path) as conn:
                    self.refresh(conn)
            except Exception:                  # never let spelling break a search
                log.exception("could not gather the library's words")
            finally:
                self._building = False

        threading.Thread(target=build, name="spelling", daemon=True).start()

    def checked_fix(self, q: str, finds_as_typed) -> str:
        """fix(q), but only when q as typed finds nothing by words (finds_as_typed(q) -> bool, remembered
        for a minute): a word that is in some photo's file name stays as it is."""
        better = self.fix(q)
        if better == q:
            return q
        now = time.monotonic()
        seen = self._checked.get(q)
        if seen is None or now - seen[0] > 60:
            seen = (now, bool(finds_as_typed(q)))
            if len(self._checked) > 500:
                self._checked.clear()
            self._checked[q] = seen
        return q if seen[1] else better

    def known(self, token: str) -> bool:
        lib = self.library
        if lib is not None and (token in lib.counts or any(token in w for w in lib.words)):
            return True
        return self.english is not None and token in self.english.counts

    def correct(self, token: str) -> str | None:
        """A better word for a search word that finds nothing and isn't English, or None."""
        return _correct(self, token)

    def fix(self, q: str) -> str:
        """q with its misspelled words corrected (the rest left exactly as typed)."""
        from . import dates

        if not q.strip() or self.library is None:
            return q
        out = []
        changed = False
        for part in re.split(r"(\s+)", q):
            low = part.lower()
            if (part.isalpha() and len(part) >= 4 and low not in dates.STOPWORDS and low not in _RELATED
                    and not dates.parse(low)[0]):
                better = self.correct(low)
                if better:
                    out.append(better)
                    changed = True
                    continue
            out.append(part)
        return "".join(out) if changed else q

    def rarity(self, token: str) -> float:
        """Inverse document frequency of a search word: rare words weigh more (1 for the commonest)."""
        lib = self.library
        n = lib.counts.get(token, 0) if lib is not None else 0
        return 1.0 + math.log((self.total + 1) / (n + 1))


@lru_cache(maxsize=1024)
def _correct(speller: Speller, token: str) -> str | None:
    if speller.known(token):
        return None
    limit = allowed_typos(token)
    if not limit:
        return None
    lib = speller.library
    found = lib.candidates(token, limit) if lib is not None else []
    if found:   # the library's own words first: they find photos. Nearest, then the most used.
        return min(found, key=lambda x: (x[0], -lib.counts[x[1]], x[1]))[1]
    if speller.english is not None:
        found = speller.english.candidates(token, limit)
        if found:
            return min(found, key=lambda x: (x[0], x[1]))[1]
    return None
