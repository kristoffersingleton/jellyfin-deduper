"""detector.py — Duplicate grouping: TMDB/TVDB match + fuzzy filename + union-find."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Dict, List, Optional

from scanner import MediaFile

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class DuplicateGroup:
    method: str          # "tmdb", "tvdb", "fuzzy"
    match_key: str       # ID or normalized name
    files: List[MediaFile]
    best: Optional[MediaFile] = None
    to_trash: List[MediaFile] = field(default_factory=list)
    similarity: float = 1.0

    def compute_best(self) -> None:
        """Rank files by quality and set best/to_trash."""
        ranked = sorted(self.files, key=lambda mf: mf.quality_key, reverse=True)
        self.best = ranked[0]
        self.to_trash = ranked[1:]


# ---------------------------------------------------------------------------
# Union-Find (for transitive fuzzy clusters)
# ---------------------------------------------------------------------------


class UnionFind:
    def __init__(self):
        self._parent: Dict[int, int] = {}

    def find(self, x: int) -> int:
        if x not in self._parent:
            self._parent[x] = x
        if self._parent[x] != x:
            self._parent[x] = self.find(self._parent[x])
        return self._parent[x]

    def union(self, x: int, y: int) -> None:
        rx, ry = self.find(x), self.find(y)
        if rx != ry:
            self._parent[ry] = rx


# ---------------------------------------------------------------------------
# Name normalization
# ---------------------------------------------------------------------------

# Quality/release tokens to strip
_QUALITY_RE = re.compile(
    r"\b("
    r"2160p|1080p|720p|480p|4k|uhd|hdr|sdr|hdr10|dv|"
    r"bluray|blu-ray|bdrip|brrip|dvdrip|dvd|hdtv|webrip|web-dl|webdl|"
    r"hevc|h264|h265|x264|x265|xvid|divx|avc|"
    r"aac|ac3|dts|atmos|truehd|flac|mp3|"
    r"extended|theatrical|remastered|proper|repack|retail|"
    r"remux|imax|3d|sbs|hs|"
    r"yts|rarbg|yify|ntb|ftw|sparks|"
    r"\d{3,4}p"
    r")\b",
    re.IGNORECASE,
)

_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_EPISODE_RE = re.compile(r"\bs(\d{1,2})e(\d{1,2})\b", re.IGNORECASE)
_NONALPHA_RE = re.compile(r"[._\-]+")
_MULTI_SPACE_RE = re.compile(r"\s+")


def _normalize(stem: str) -> str:
    s = stem.lower()
    s = _NONALPHA_RE.sub(" ", s)
    s = _QUALITY_RE.sub("", s)
    s = _YEAR_RE.sub("", s)
    s = _MULTI_SPACE_RE.sub(" ", s).strip()
    return s


def _episode_key(stem: str) -> Optional[str]:
    """Return 'SxxExx' episode code if found, else None."""
    m = _EPISODE_RE.search(stem)
    if m:
        return f"s{int(m.group(1)):02d}e{int(m.group(2)):02d}"
    return None


# ---------------------------------------------------------------------------
# Detection passes
# ---------------------------------------------------------------------------


def detect_by_provider_ids(files: List[MediaFile]) -> List[DuplicateGroup]:
    """Group files sharing the same TMDB or TVDB provider ID."""
    groups: List[DuplicateGroup] = []

    for provider in ("tmdb", "tvdb"):
        buckets: Dict[str, List[MediaFile]] = {}
        for mf in files:
            pid = mf.provider_ids.get(provider)
            if pid:
                buckets.setdefault(pid, []).append(mf)

        for pid, members in buckets.items():
            if len(members) >= 2:
                g = DuplicateGroup(method=provider, match_key=pid, files=members)
                groups.append(g)

    return groups


def _bucket_key(norm: str, ep_key: Optional[str]) -> str:
    """
    Build a coarse grouping key to avoid O(n²) comparisons.

    Strategy:
    - TV episodes: use episode code (s01e03) as bucket — only compare
      files that share the exact SxEx designation.
    - Everything else: use first 1-2 words of the normalized name.
      Files with different opening words can't score >= 0.85 unless
      they're very short, so this is safe for any realistic threshold.
    """
    if ep_key:
        return ep_key
    words = norm.split()
    if not words:
        return ""
    # Use first two words as bucket (falls back to one if only one exists)
    return " ".join(words[:2])


def detect_by_fuzzy_name(
    files: List[MediaFile],
    threshold: float = 0.85,
    already_grouped: Optional[set] = None,
) -> List[DuplicateGroup]:
    """Find duplicates via fuzzy filename matching + union-find clustering.

    Uses first-word bucketing to avoid O(n²) comparisons on large libraries.
    """
    # Exclude files already handled by provider-ID pass
    candidates = [
        mf for mf in files
        if already_grouped is None or id(mf) not in already_grouped
    ]

    norms = [_normalize(mf.stem) for mf in candidates]
    ep_keys = [_episode_key(mf.stem) for mf in candidates]
    n = len(candidates)

    # Build buckets: only compare within the same bucket
    buckets: Dict[str, List[int]] = {}
    for idx in range(n):
        key = _bucket_key(norms[idx], ep_keys[idx])
        if key:
            buckets.setdefault(key, []).append(idx)

    uf = UnionFind()

    for bucket_indices in buckets.values():
        if len(bucket_indices) < 2:
            continue
        for a in range(len(bucket_indices)):
            for b in range(a + 1, len(bucket_indices)):
                i, j = bucket_indices[a], bucket_indices[b]

                # TV episode guard: must share same SxEx code if both have one
                ei, ej = ep_keys[i], ep_keys[j]
                if ei and ej and ei != ej:
                    continue

                si, sj = norms[i], norms[j]
                if not si or not sj:
                    continue

                # Length ratio pre-filter: if strings differ too much in length
                # they can't score >= threshold
                len_ratio = min(len(si), len(sj)) / max(len(si), len(sj))
                if len_ratio < threshold:
                    continue

                sm = SequenceMatcher(None, si, sj)
                if sm.quick_ratio() < threshold:
                    continue
                if sm.ratio() >= threshold:
                    uf.union(i, j)

    # Collect clusters
    clusters: Dict[int, List[int]] = {}
    for idx in range(n):
        root = uf.find(idx)
        clusters.setdefault(root, []).append(idx)

    groups: List[DuplicateGroup] = []
    for indices in clusters.values():
        if len(indices) < 2:
            continue
        members = [candidates[i] for i in indices]
        # Use normalized name of first member as key
        key = norms[indices[0]]
        # Compute average similarity for the group
        similarities = []
        for a in range(len(indices)):
            for b in range(a + 1, len(indices)):
                sm = SequenceMatcher(None, norms[indices[a]], norms[indices[b]])
                similarities.append(sm.ratio())
        avg_sim = sum(similarities) / len(similarities) if similarities else 1.0
        g = DuplicateGroup(
            method="fuzzy",
            match_key=key,
            files=members,
            similarity=avg_sim,
        )
        groups.append(g)

    return groups


def find_duplicates(
    files: List[MediaFile],
    fuzzy_threshold: float = 0.85,
    use_api: bool = True,
) -> List[DuplicateGroup]:
    """Run all detection passes and return merged list of DuplicateGroups."""
    groups: List[DuplicateGroup] = []

    if use_api:
        api_groups = detect_by_provider_ids(files)
        groups.extend(api_groups)
    else:
        api_groups = []

    # Build set of file IDs already assigned by API pass
    api_file_ids: set = set()
    for g in api_groups:
        for mf in g.files:
            api_file_ids.add(id(mf))

    fuzzy_groups = detect_by_fuzzy_name(
        files,
        threshold=fuzzy_threshold,
        already_grouped=api_file_ids,
    )
    groups.extend(fuzzy_groups)

    # Rank each group
    for g in groups:
        g.compute_best()

    return groups
