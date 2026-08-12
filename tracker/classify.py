"""Weighted regex taxonomy. Every item is scored against every enabled
category; the highest score wins, and anything below classifier.min_score
is dropped as noise. SEC 8-K item codes bypass keyword scoring entirely —
config.yaml's sec_item_map is a fact, not an inference.
"""
from __future__ import annotations

import re
from functools import lru_cache

from .models import ClassifiedItem, RawItem


def _compile_categories(categories: dict) -> dict:
    compiled = {}
    for key, cat in (categories or {}).items():
        if not cat.get("enabled", True):
            continue
        patterns = []
        for pat, weight in cat.get("patterns", []):
            try:
                patterns.append((re.compile(pat, re.IGNORECASE), float(weight)))
            except re.error as exc:
                print(f"[classify] bad pattern in {key!r}: {pat!r} ({exc})")
        compiled[key] = {"label": cat.get("label", key), "patterns": patterns}
    return compiled


class Classifier:
    def __init__(self, config: dict):
        clf = config.get("classifier", {}) or {}
        self.min_score = float(clf.get("min_score", 2.0))
        self.categories = _compile_categories(clf.get("categories", {}))
        self.sec_item_map = config.get("sec_item_map", {}) or {}

    def category_labels(self) -> dict:
        return {key: c["label"] for key, c in self.categories.items()}

    def classify(self, item: RawItem) -> ClassifiedItem | None:
        # SEC 8-K item codes are an exact fact, not a keyword guess.
        if item.sec_item:
            for code in str(item.sec_item).split(","):
                code = code.strip()
                cat_key = self.sec_item_map.get(code)
                if cat_key and cat_key in self.categories:
                    label = self.categories[cat_key]["label"]
                    return ClassifiedItem(raw=item, category=cat_key, label=label,
                                           score=99.0, matched_on=[f"8-K item {code}"])

        # classify() runs on the thread that called it (the pipeline's main
        # thread, for a freshly-fetched item) with the GIL held for the
        # whole duration of each pattern.search() — unlike a slow network
        # call, nothing else in the process can make progress while one of
        # these is running, and no run-level deadline can interrupt it
        # either. Patterns here use bounded quantifiers like .{0,120}, so
        # match cost scales with input length; capping that length bounds
        # the worst case no matter how large upstream text gets. (Fetchers
        # already cap title/summary at the source for the same reason —
        # this is the last line of defense, not the only one.)
        text = f"{item.title}\n{item.summary}"[:2000]
        best_key, best_score, best_matches = None, 0.0, []
        for key, cat in self.categories.items():
            score = 0.0
            matches = []
            for pattern, weight in cat["patterns"]:
                m = pattern.search(text)
                if m:
                    score += weight
                    matches.append(m.group(0).strip())
            if score > best_score:
                best_key, best_score, best_matches = key, score, matches

        if best_key is None or best_score < self.min_score:
            return None
        return ClassifiedItem(raw=item, category=best_key,
                               label=self.categories[best_key]["label"],
                               score=best_score, matched_on=best_matches)
