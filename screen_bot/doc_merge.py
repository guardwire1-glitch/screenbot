"""doc_merge.py - merge lecture frames into a master without repeating existing code.

Rules: unseen != deleted; in-view + absent (N stable frames) == deleted; a complete
class/function on screen REPLACES the same-named entity (never appended twice);
nothing is removed without a snapshot; plan_injection() only adds what the target
file is missing and never touches code that is already there.
"""
from __future__ import annotations

import difflib
import hashlib
import re
from collections import deque
from dataclasses import dataclass, field
from typing import List, Optional

UNREADABLE = "«UNREADABLE»"
BAD_TEXT = ("_SB_NO_SELECTION_", "Micro-edits applied", "[ScreenBot]")
RISKY_FLAGS = {"folded", "wrapped", "hscroll", "low_quality"}
_Q = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'", "\u00a0": " ", "\u200b": ""})
_CTRL = {"if", "for", "while", "switch", "catch", "else", "return", "new", "try", "do",
         "throw", "case", "using", "lock", "foreach", "synchronized"}
_CLASS = re.compile(r"(?:^|[\s)])(class|interface|enum|record|struct|namespace)\s+([A-Za-z_]\w*)")
_FUNC = re.compile(r"^(?:[\w@<>\[\],.?*&:]+\s+)*?([A-Za-z_]\w*)\s*\(([^()]*)\)\s*(?:throws\s+[\w.,\s]+)?\s*\{?$")


def norm(t: str) -> str:
    return re.sub(r"\s+", " ", t.translate(_Q)).strip()


def weak(n: str) -> bool:
    return re.fullmatch(r"[{}()\[\];,\s]*", n) is not None


@dataclass
class FrameLine:
    text: str
    line_no: Optional[int] = None
    visible: str = "full"
    confidence: float = 1.0


@dataclass
class Meta:
    file_name: Optional[str] = None
    quality_ok: bool = True
    overlay: bool = False          # toast/tooltip/popup on screen
    flags: tuple = ()              # folded / wrapped / hscroll / low_quality
    top: Optional[bool] = None     # top of file visible (None -> unknown, never inferred from viewport numbers)
    bottom: bool = False           # blank editor area below last line is visible
    allow_destructive: bool = True # live OCR should pass False; only verified edits may delete


@dataclass
class Cfg:
    min_conf: float = 0.85
    req_insert: int = 2            # set 1 if LineConsensusTracker already gates upstream
    req_modify: int = 2
    req_delete: int = 2
    req_rewrite: int = 3
    large_delete: int = 12
    max_delete: int = 40
    history: int = 50


@dataclass
class ReviewChange:
    """A confirmed lecturer edit that was deliberately NOT applied to the master.

    The live path passes allow_destructive=False, so replacements and deletions
    must surface as reviewable changes instead of being dropped. `old`/`new` are
    raw source lines (blank and structural lines included) for display.
    """
    old: List[str] = field(default_factory=list)
    new: List[str] = field(default_factory=list)
    kind: str = "edit"             # edit|delete|replacement


@dataclass
class Result:
    code: str
    event: str = "noop"            # noop|insert|delete|modify|entity|rewrite|review|held|blocked|low_quality
    inserted: List[str] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)
    held: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    reviews: List[ReviewChange] = field(default_factory=list)
    review_code: str = ""          # full document with held edits applied (review candidate)

    @property
    def changed(self) -> bool:
        return self.event in {"insert", "delete", "modify", "entity", "rewrite"}


def _strip(line: str, blk: bool):
    out, i, q = [], 0, None
    while i < len(line):
        c = line[i]
        if blk:
            if line.startswith("*/", i):
                blk, i = False, i + 2
            else:
                i += 1
        elif q:
            if c == "\\":
                i += 2
                continue
            q = None if c == q else q
            i += 1
        elif line.startswith("//", i):
            break
        elif line.startswith("/*", i):
            blk, i = True, i + 2
        else:
            if c in "\"'":
                q = c
            out.append(c if not q else "")
            i += 1
    return "".join(out), blk


def _header(s: str) -> Optional[str]:
    m = _CLASS.search(s)
    if m:
        return f"class {m.group(2)}"
    if s.split()[0] in _CTRL or s.endswith(";"):
        return None
    m = _FUNC.match(s)
    if not m or m.group(1) in _CTRL:
        return None
    ps = [" ".join(p.split()[:-1]) or p.strip() for p in m.group(2).split(",") if p.strip()]
    return f"func {m.group(1)}({','.join(ps)})"


def entities(lines: List[str]):
    """Complete top-level classes/functions as (key, start, end). Brace languages.
    Only column-0 headers count, and an unmatched '}' means the view started mid-block,
    so everything found before it is discarded (prevents scrolled fragments looking like
    new top-level entities)."""
    out, depth, blk, cur, pend = [], 0, False, None, None
    for i, raw in enumerate(lines):
        code, blk = _strip(raw, blk)
        s = code.strip()
        if not s:
            continue
        if cur is None:
            if pend and s.startswith("{"):
                cur, pend = [pend[0], pend[1], depth], None
            else:
                pend = None
                k = _header(s) if depth == 0 and not s.startswith("}") and raw[:1] not in " \t" else None
                if k and "{" in s:
                    cur = [k, i, depth]
                elif k and not s.endswith(";"):
                    pend = (k, i)
        for ch in s:
            if ch == "{":
                depth += 1
            elif ch == "}":
                if depth == 0:
                    out.clear()
                depth = max(0, depth - 1)
                if cur and depth == cur[2]:
                    out.append((cur[0], cur[1], i))
                    cur = None
    return out


def _strong(ls):
    return [n for n in (norm(x) for x in ls) if n and not weak(n)]


def _hash(ls) -> str:
    return hashlib.md5("\n".join(_strong(ls)).encode()).hexdigest()[:10]


class DocMerger:
    def __init__(self, file_name: Optional[str] = None, cfg: Optional[Cfg] = None):
        self.file_name, self.cfg = file_name, cfg or Cfg()
        self.lines: List[str] = []
        self.history: List[List[str]] = []
        self._pend: dict = {}
        self._rev_pend: dict = {}
        self._rev_seen: set = set()
        self._n = 0
        self._dframes: deque = deque(maxlen=10)
        self._freeze = 0

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    def seed_baseline(self, text: str) -> None:
        """Load code that already exists in the file so it is never re-added."""
        self.lines = text.split("\n") if text else []
        self._pend.clear()
        self._rev_pend.clear()
        self._rev_seen.clear()

    def undo(self) -> bool:
        if not self.history:
            return False
        self.lines = self.history.pop()
        self._pend.clear()
        return True

    # ------------------------------------------------------------------
    def _ok(self, l: FrameLine) -> bool:
        t = l.text or ""
        if l.visible != "full" or UNREADABLE in t or any(b in t for b in BAD_TEXT):
            return False
        if not norm(t):
            return True
        return l.confidence is None or l.confidence >= self.cfg.min_conf

    def merge(self, frame, meta: Optional[Meta] = None) -> Result:
        meta, cfg = meta or Meta(), self.cfg
        r = Result(self.text)
        if not meta.quality_ok:
            r.event = "low_quality"
            return r
        meta_name = (meta.file_name or "").replace("\\", "/").split("/")[-1].strip().lower()
        doc_name = (self.file_name or "").replace("\\", "/").split("/")[-1].strip().lower()
        if meta_name and doc_name and meta_name != doc_name:
            r.event, r.notes = "blocked", ["file_mismatch"]
            return r
        self._n += 1
        fl = [x if isinstance(x, FrameLine) else FrameLine(
            getattr(x, "text", ""), getattr(x, "line_no", None),
            getattr(x, "visible", "full"), float(getattr(x, "confidence", 1.0))) for x in frame]
        ok = [self._ok(x) for x in fl]
        tx = [x.text if o else "" for x, o in zip(fl, ok)]
        risky = bool(set(meta.flags) & RISKY_FLAGS)
        destr = (
            meta.allow_destructive
            and not meta.overlay
            and not risky
            and self._n >= self._freeze
        )
        # The live path passes allow_destructive=False. A trustworthy frame still
        # describes a real lecturer edit, so it must surface as a review proposal
        # instead of being silently skipped. Overlay/folded/wrapped frames stay
        # ignored: their text is not reliable enough to propose a deletion.
        review_ok = not meta.allow_destructive and not meta.overlay and not risky
        # A viewport line number of 1 is not proof that the file's first line is
        # on screen (OCR gutters restart at 1). Unknown means unknown: additions
        # are still allowed, deletions are not.
        top = meta.top if meta.top is not None else False
        ch: list = []   # (start, end, new, sig, required, destructive, is_entity)

        # 1) complete entities on screen replace/insert by name -------------------
        mm = {k: (a, b) for k, a, b in entities(self.lines)}
        fe = entities(tx)
        used = [False] * len(fl)
        for idx, (k, a, b) in enumerate(fe):
            if not all(ok[a:b + 1]):
                continue
            used[a:b + 1] = [True] * (b - a + 1)
            new = tx[a:b + 1]
            if k not in mm:
                nxt = next((mm[kk][0] for kk, _, _ in fe[idx + 1:] if kk in mm), len(self.lines))
                ch.append((nxt, nxt, new, ("ei", k, _hash(new)), cfg.req_insert, False, True))
                continue
            s, e = mm[k]
            old = self.lines[s:e + 1]
            os_, ns = _strong(old), _strong(new)
            if os_ == ns:
                continue
            m = sum(z.size for z in difflib.SequenceMatcher(None, os_, ns, autojunk=False).get_matching_blocks())
            additive = m == len(os_)
            rewrite = m <= max(len(os_), len(ns)) / 2 or len(ns) < len(os_) / 2
            req = cfg.req_insert if additive else cfg.req_rewrite if rewrite else cfg.req_modify
            ch.append((s, e + 1, new, ("er", k, _hash(new)), req, not additive, True))

        # 2) loose lines: anchored line merge ------------------------------------
        held: List[str] = []
        run: List[int] = []
        for i in range(len(fl) + 1):
            if i < len(fl) and ok[i] and not used[i]:
                run.append(i)
                continue
            if run:
                self._run(run, tx, top and run[0] == 0, meta.bottom and run[-1] == len(fl) - 1, ch, held)
            run = []

        # 3) persistence gate -------------------------------------------------------
        applied, review, newp, newrp = [], [], {}, {}
        seen_now = set()
        for c in ch:
            if c[5]:
                if destr:
                    cnt = self._pend.get(c[3], 0) + 1
                    if cnt >= c[4]:
                        applied.append(c)
                    else:
                        newp[c[3]] = cnt
                elif review_ok:
                    # A confirmed review is not applied to the master, so the same
                    # observation arrives every frame. Remember it as confirmed
                    # instead of making the proposal flicker in and out.
                    if c[3] in self._rev_seen:
                        review.append(c)
                        seen_now.add(c[3])
                        continue
                    cnt = self._rev_pend.get(c[3], 0) + 1
                    if cnt >= c[4]:
                        review.append(c)
                        seen_now.add(c[3])
                    else:
                        newrp[c[3]] = cnt
                else:
                    r.notes.append("destructive_skipped")
                continue
            cnt = self._pend.get(c[3], 0) + 1
            if cnt >= c[4]:
                applied.append(c)
            else:
                newp[c[3]] = cnt
        self._pend = newp
        self._rev_pend = newrp
        self._rev_seen = seen_now
        r.held = held
        if not applied and not review:
            r.event = "held" if (newp or newrp or held) else "noop"
            return r

        # 4) resolve overlaps, apply additive changes, hold edits for review -------
        applied.sort(key=lambda c: (not c[6], c[0]))
        cov, keep = [], []
        for c in applied:
            if any(c[0] < e and c[1] > s or s < c[0] < e for s, e in cov):
                continue
            keep.append(c)
            if c[1] > c[0]:
                cov.append((c[0], c[1]))

        review.sort(key=lambda c: (c[0], c[1]))
        rev_cov, rev_keep = [], []
        for c in review:
            if any(c[0] < e and c[1] > s or s < c[0] < e for s, e in cov):
                continue
            if any(c[0] < e and c[1] > s or s < c[0] < e for s, e in rev_cov):
                continue
            rev_keep.append(c)
            rev_cov.append((c[0], c[1]))

        orig = list(self.lines)
        cand = list(orig)
        for c in sorted(keep, key=lambda c: (c[0], c[1]), reverse=True):
            cand[c[0]:c[1]] = c[2]
        if keep:
            keys = [k for k, _, _ in entities(cand)]
            before, after = len(_strong(orig)), len(_strong(cand))
            if len(keys) != len(set(keys)):
                r.event, r.notes = "blocked", ["duplicate_declaration_blocked"]
                return r
            if before >= 5 and after < before * 0.4 and not any(c[4] >= cfg.req_rewrite for c in keep):
                r.event, r.notes = "blocked", ["shrink_blocked"]
                return r
            removed = [x for c in keep for x in orig[c[0]:c[1]] if norm(x)]
            if any(c[5] for c in keep):
                self._dframes.append(self._n)
                if len([n for n in self._dframes if n > self._n - 10]) > 4:
                    self._freeze = self._n + 20
                    r.notes.append("breaker_tripped")
                self.history.append(list(self.lines))
            del self.history[:-cfg.history]
            self.lines = cand
        else:
            removed = []

        review_items: List[ReviewChange] = []
        for c in rev_keep:
            old = list(orig[c[0]:c[1]])
            new = list(c[2])
            kind = "replacement" if c[6] else ("deletion" if not any(norm(x) for x in new) else "edit")
            review_items.append(ReviewChange(old=old, new=new, kind=kind))

        r.code = self.text
        r.inserted = [x for c in keep for x in c[2] if norm(x) and x not in removed]
        r.removed = [x for x in removed if x not in r.inserted]
        r.reviews = review_items
        if review_items:
            rev_cand = list(orig)
            for c in sorted(keep + rev_keep, key=lambda c: (c[0], c[1]), reverse=True):
                rev_cand[c[0]:c[1]] = c[2]
            r.review_code = "\n".join(rev_cand)
        if not keep:
            r.event = "review"
        else:
            r.event = ("rewrite" if any(c[6] and c[4] >= cfg.req_rewrite for c in keep) else
                       "entity" if any(c[6] for c in keep) else
                       "delete" if r.removed and not r.inserted else
                       "modify" if r.removed else "insert")
        return r

    # ------------------------------------------------------------------
    def _run(self, idx, tx, top, bot, ch, held):
        cfg, L = self.cfg, self.lines
        rt = [tx[i] for i in idx]
        mn, fn = [norm(x) for x in L], [norm(x) for x in rt]
        ms = [i for i, n in enumerate(mn) if not weak(n)]
        fs = [j for j, n in enumerate(fn) if not weak(n)]
        if not fs:
            return
        if not ms:
            ch.append((len(L), len(L), rt, ("i0", tuple(fn)), cfg.req_insert, False, False))
            return
        sm = difflib.SequenceMatcher(None, [mn[i] for i in ms], [fn[j] for j in fs], autojunk=False)
        A = [(ms[a + t], fs[b + t]) for a, b, s in sm.get_matching_blocks() for t in range(s)]
        if not A:   # no overlap with anything known: hold, append only if stable for req_rewrite frames
            held.extend(rt[j] for j in fs)
            ch.append((len(L), len(L), rt, ("iu", tuple(fn)), cfg.req_rewrite, False, False))
            return
        B = [(-1, -1)] + A + [(len(L), len(rt))]
        for k in range(len(B) - 1):
            (m0, f0), (m1, f1) = B[k], B[k + 1]
            self._region(m0 + 1, m1, f0 + 1, f1, mn, fn, rt,
                         k == 0 and not top, k == len(B) - 2 and not bot, ch)

    def _region(self, m0, m1, f0, f1, mn, fn, rt, lead_free, trail_free, ch):
        cfg, L = self.cfg, self.lines
        M, F = mn[m0:m1], fn[f0:f1]
        if not M and not F:
            return
        ops = difflib.SequenceMatcher(None, M, F, autojunk=False).get_opcodes()
        se = [(a, b) for t, a, b, _, _ in ops if t == "equal" and any(not weak(x) for x in M[a:b])]
        lo = min((a for a, _ in se), default=len(M))
        hi = max((b for _, b in se), default=0)

        def ins(pos, new):
            if any(x.strip() for x in new):
                ch.append((pos, pos, list(new), ("i", mn[pos - 1] if pos else "", tuple(norm(x) for x in new)),
                           cfg.req_insert, False, False))

        for t, i1, i2, j1, j2 in ops:
            if t == "equal":
                continue
            new = rt[f0 + j1:f0 + j2]
            if lead_free and i2 <= lo:          # master side not on screen: only add
                ins(m0 + i2, new)
                continue
            if trail_free and i1 >= hi:
                ins(m0 + i1, new)
                continue
            old = L[m0 + i1:m0 + i2]
            ob, nb = not any(x.strip() for x in old), not any(x.strip() for x in new)
            if t == "insert" or ob:
                ins(m0 + i2, new)
                continue
            gone = len(_strong(old))
            if gone > cfg.max_delete:
                continue                         # too big to trust: leave for review
            req = cfg.req_rewrite if gone > cfg.large_delete else (cfg.req_delete if nb or t == "delete" else cfg.req_modify)
            body = [] if nb or t == "delete" else new
            ch.append((m0 + i1, m0 + i2, body, ("r", tuple(norm(x) for x in old), tuple(norm(x) for x in body)),
                       req, True, False))


# ----------------------------------------------------------------------
def plan_injection(master: str, target: str):
    """What must be ADDED to the target file so it contains master. Never deletes or
    repeats code the target already has. Returns (steps, conflicts); steps = [(index, lines)]
    sorted bottom-to-top so they can be applied without shifting positions."""
    if not target.strip():
        return [(0, master.split("\n"))], []
    M, T = master.split("\n"), target.split("\n")
    m_norm = [norm(x) for x in M]
    t_norm = [norm(x) for x in T]
    matcher = difflib.SequenceMatcher(lambda x: weak(x), t_norm, m_norm, autojunk=False)
    ops = matcher.get_opcodes()
    steps, conflicts = [], []
    for t, i1, i2, j1, j2 in ops:
        new = M[j1:j2]
        if not any(norm(x) for x in new):
            continue
        if t == "insert":
            steps.append((i1, new))
        elif t == "replace":
            target_slice = [t_norm[k] for k in range(i1, i2)]
            if all(weak(x) for x in target_slice):
                steps.append(("replace", i1, i2, new))
            elif all(not norm(x) for x in target_slice):
                steps.append((i2, new))
            else:
                conflicts.append((i1, i2, new))   # user wrote something different here: ask, don't overwrite

    def _pos(s):
        return s[1] if (isinstance(s, tuple) and len(s) == 4 and s[0] == "replace") else s[0]

    return sorted(steps, key=_pos, reverse=True), conflicts


def apply_plan(target: str, steps) -> str:
    T = target.split("\n")
    for s in steps:
        if isinstance(s, tuple) and len(s) == 4 and s[0] == "replace":
            _, i1, i2, new = s
            T[i1:i2] = new
        else:
            pos, new = s
            T[pos:pos] = new
    return "\n".join(T)


def _normalized_lines(text: str) -> List[str]:
    clean = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    return clean.split("\n")


def _line_positions(source: List[str], target: List[str]) -> dict:
    """Map line indexes of `source` to their in-order matches in `target`."""
    mapping = {}
    matcher = difflib.SequenceMatcher(None, source, target, autojunk=False)
    for a, b, size in matcher.get_matching_blocks():
        for offset in range(size):
            mapping[a + offset] = b + offset
    return mapping


def _has_block(lines: List[str], block: List[str]) -> bool:
    if not block:
        return True
    size = len(block)
    return any(lines[k:k + size] == block for k in range(len(lines) - size + 1))


def plan_review_changes(base_master: str, review_master: str, target: str):
    """Translate confirmed edits (base_master -> review_master) onto `target`.

    Only a region whose old lines are still present unchanged in the target is
    rewritten. If the user changed that region itself, nothing is proposed:
    a human must merge it. Returns (proposed_text_or_None, problems).
    """
    if ([norm(x) for x in _normalized_lines(base_master)]
            == [norm(x) for x in _normalized_lines(review_master)]):
        return (target or ""), []
    doc = _normalized_lines(base_master)
    rev = _normalized_lines(review_master)
    tgt = _normalized_lines(target)
    doc_n = [norm(x) for x in doc]
    rev_n = [norm(x) for x in rev]
    tgt_n = [norm(x) for x in tgt]
    d2t = _line_positions(doc_n, tgt_n)
    edits, problems = [], []
    matcher = difflib.SequenceMatcher(None, doc_n, rev_n, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        new = list(rev[j1:j2])
        if i1 == i2:
            if i1 == 0:
                pos = d2t.get(0)
                if pos is None and not any(tgt_n):
                    pos = 0
            else:
                anchor = d2t.get(i1 - 1)
                pos = anchor + 1 if anchor is not None else None
            if pos is None:
                problems.append(f"new lines cannot be anchored near line {i1 + 1}")
            else:
                edits.append((pos, pos, new))
            continue
        span = [d2t.get(idx) for idx in range(i1, i2)]
        if any(pos is None for pos in span):
            problems.append(f"edited lines {i1 + 1}-{i2} are not present unchanged in the file")
            continue
        if any(span[k + 1] != span[k] + 1 for k in range(len(span) - 1)):
            problems.append(f"edited lines {i1 + 1}-{i2} have local edits in the file")
            continue
        edits.append((span[0], span[-1] + 1, new))
    if problems:
        return None, problems
    out = list(tgt)
    for start, end, new in sorted(edits, key=lambda e: (e[0], e[1]), reverse=True):
        out[start:end] = new
    return "\n".join(out), []


def review_state_present(base_master: str, review_master: str, target: str) -> bool:
    """True when the target already embodies the lecture's edit.

    Conservative: every removed strong line must be gone and every introduced
    strong line present as a contiguous block. Used to resync after a human
    accepts the review (the scan otherwise keeps re-detecting the same edit).
    """
    if ([norm(x) for x in _normalized_lines(base_master)]
            == [norm(x) for x in _normalized_lines(review_master)]):
        return False
    doc_n = [norm(x) for x in _normalized_lines(base_master)]
    rev_n = [norm(x) for x in _normalized_lines(review_master)]
    tgt_n = [norm(x) for x in _normalized_lines(target)]
    tgt_strong = [x for x in tgt_n if x and not weak(x)]
    matcher = difflib.SequenceMatcher(None, doc_n, rev_n, autojunk=False)
    saw_change = False
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        saw_change = True
        old_strong = [x for x in doc_n[i1:i2] if x and not weak(x)]
        new_strong = [x for x in rev_n[j1:j2] if x and not weak(x)]
        if i1 < i2 and not old_strong:
            return False       # cannot prove a weak-only deletion happened
        if any(line in tgt_strong for line in old_strong):
            return False       # still present somewhere: not the reviewed state
        if new_strong and not _has_block(tgt_strong, new_strong):
            return False
    return saw_change


class Registry:
    """One DocMerger per file so tabs never mix."""
    def __init__(self, cfg: Optional[Cfg] = None):
        self.cfg, self.docs = cfg, {}

    def get(self, file_name: str) -> DocMerger:
        k = (file_name or "untitled").replace("\\", "/").split("/")[-1].lower()
        return self.docs.setdefault(k, DocMerger(k, self.cfg))
