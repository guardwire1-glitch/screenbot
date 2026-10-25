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
class Result:
    code: str
    event: str = "noop"            # noop|insert|delete|modify|entity|rewrite|held|blocked|low_quality
    inserted: List[str] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)
    held: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

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
        destr = (
            meta.allow_destructive
            and not meta.overlay
            and not (set(meta.flags) & RISKY_FLAGS)
            and self._n >= self._freeze
        )
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
        applied, newp = [], {}
        for c in ch:
            if c[5] and not destr:
                r.notes.append("destructive_skipped")
                continue
            cnt = self._pend.get(c[3], 0) + 1
            if cnt >= c[4]:
                applied.append(c)
            else:
                newp[c[3]] = cnt
        self._pend = newp
        r.held = held
        if not applied:
            r.event = "held" if (newp or held) else "noop"
            return r

        # 4) resolve overlaps, apply on a copy, check invariants --------------------
        applied.sort(key=lambda c: (not c[6], c[0]))
        cov, keep = [], []
        for c in applied:
            if any(c[0] < e and c[1] > s or s < c[0] < e for s, e in cov):
                continue
            keep.append(c)
            if c[1] > c[0]:
                cov.append((c[0], c[1]))
        cand = list(self.lines)
        for c in sorted(keep, key=lambda c: (c[0], c[1]), reverse=True):
            cand[c[0]:c[1]] = c[2]
        keys = [k for k, _, _ in entities(cand)]
        before, after = len(_strong(self.lines)), len(_strong(cand))
        if len(keys) != len(set(keys)):
            r.event, r.notes = "blocked", ["duplicate_declaration_blocked"]
            return r
        if before >= 5 and after < before * 0.4 and not any(c[4] >= cfg.req_rewrite for c in keep):
            r.event, r.notes = "blocked", ["shrink_blocked"]
            return r
        removed = [x for c in keep for x in self.lines[c[0]:c[1]] if norm(x)]
        if any(c[5] for c in keep):
            self._dframes.append(self._n)
            if len([n for n in self._dframes if n > self._n - 10]) > 4:
                self._freeze = self._n + 20
                r.notes.append("breaker_tripped")
        self.history.append(list(self.lines))
        del self.history[:-cfg.history]
        self.lines = cand
        r.code = self.text
        r.inserted = [x for c in keep for x in c[2] if norm(x) and x not in removed]
        r.removed = [x for x in removed if x not in r.inserted]
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


class Registry:
    """One DocMerger per file so tabs never mix."""
    def __init__(self, cfg: Optional[Cfg] = None):
        self.cfg, self.docs = cfg, {}

    def get(self, file_name: str) -> DocMerger:
        k = (file_name or "untitled").replace("\\", "/").split("/")[-1].lower()
        return self.docs.setdefault(k, DocMerger(k, self.cfg))
