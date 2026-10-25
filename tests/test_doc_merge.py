import random
from screen_bot.doc_merge import (DocMerger, FrameLine, Meta, Cfg, plan_injection,
                                  apply_plan, Registry, norm, entities, UNREADABLE,
                                  plan_review_changes, review_state_present)

V1 = """public class Main {

    public static void main(String[] args) {

        // method = a block of code
        hello();

    }

    static void hello() {
        System.out.println("Hello");
    }

}"""
V2 = """public class Main {

    public static void main(String[] args) {

        // method = a block of code

        int x = 3;

    }

}"""
V3 = V2.replace("int x = 3;", "int x = 3;\n        int y = 4;\n        add(x,y);") + ""


def fr(text, **kw):
    return [FrameLine(t, **kw) for t in text.split("\n")]


def feed(m, text, n=3, meta=None):
    r = None
    for _ in range(n):
        r = m.merge(fr(text), meta)
    return r


def sub(m):
    return [norm(x) for x in m.lines if norm(x)]


def want(t):
    return [norm(x) for x in t.split("\n") if norm(x)]


def test_first_content_and_idempotence():
    m = DocMerger()
    feed(m, V1)
    assert sub(m) == want(V1)
    assert not m.merge(fr(V1)).changed and not m.merge(fr(V1)).changed


def test_youtube_rewrite_replaces_not_duplicates():
    m = DocMerger()
    feed(m, V1)
    r = feed(m, V2)
    assert sub(m) == want(V2)
    assert m.text.count("public class Main") == 1
    assert r.event in {"rewrite", "entity", "noop"}
    feed(m, V3)
    assert sub(m) == want(V3) and m.text.count("public class Main") == 1


def test_needs_consecutive_frames_for_rewrite():
    m = DocMerger()
    feed(m, V1)
    m.merge(fr(V2)); m.merge(fr(V2))
    assert sub(m) == want(V1)            # 2 frames not enough for a rewrite
    m.merge(fr(V2))
    assert sub(m) == want(V2)


def test_existing_baseline_not_repeated():
    m = DocMerger()
    m.seed_baseline(V1)
    feed(m, V1)
    assert m.text == V1 and m.text.count("hello();") == 1
    partial = "\n".join(V1.split("\n")[:7])          # frame shows only the top part
    feed(m, partial)
    assert sub(m) == want(V1)                        # nothing lost, nothing repeated


def test_new_lines_append_once_at_anchor():
    m = DocMerger()
    feed(m, V2)
    feed(m, V3)
    assert m.text.count("int x = 3;") == 1 and "add(x,y);" in m.text


def test_scroll_off_never_deletes():
    m = DocMerger()
    feed(m, V1)
    bottom = "\n".join(V1.split("\n")[8:])
    feed(m, bottom, 5)
    assert sub(m) == want(V1)


def test_in_view_deletion_after_stable_frames():
    code = "int a = 1;\nint b = 2;\nint c = 3;\nint d = 4;\nint e = 5;"
    m = DocMerger()
    feed(m, code)
    feed(m, "int a = 1;\nint b = 2;\nint d = 4;\nint e = 5;")
    assert "int c = 3;" not in m.text and "int d = 4;" in m.text
    assert m.undo() and "int c = 3;" in m.text


def test_occlusion_and_unreadable_never_delete_or_insert():
    m = DocMerger()
    feed(m, V1)
    f = fr(V1)
    f[5] = FrameLine("hel«x»", visible="partial", confidence=0.4)
    f[10] = FrameLine(f"System.out.println({UNREADABLE});")
    for _ in range(4):
        m.merge(f)
    assert sub(m) == want(V1)
    for _ in range(4):
        m.merge(fr(V2), Meta(overlay=True))
    assert sub(m) == want(V1)            # overlay blocks destructive rewrite


def test_risky_flags_block_destructive():
    m = DocMerger()
    feed(m, V1)
    feed(m, V2, 5, Meta(flags=("folded",)))
    assert sub(m) == want(V1)


def test_low_quality_frames_ignored():
    m = DocMerger()
    assert m.merge(fr(V1), Meta(quality_ok=False)).event == "low_quality" and not m.lines


def test_unanchored_new_block_needs_three_frames():
    m = DocMerger()
    feed(m, "int a = 1;\nint b = 2;\nint c = 3;")
    z = "String s = \"x\";\nprint(s);\nboolean ok = true;"
    m.merge(fr(z)); m.merge(fr(z))
    assert "boolean ok" not in m.text
    m.merge(fr(z))
    assert "boolean ok" in m.text and "int a = 1;" in m.text


def test_file_mismatch_and_registry():
    m = DocMerger("A.java")
    assert m.merge(fr(V1), Meta(file_name="B.java")).event == "blocked"
    r = Registry()
    assert r.get("C:\\x\\Main.java") is r.get("main.java") and r.get("a.java") is not r.get("b.java")


def test_file_match_ignores_path_and_ocr_casing():
    # OCR reports the visible tab ("Demo.cs"); the registry stores "demo.cs".
    # A real frame must not be rejected as file_mismatch because of casing.
    m = DocMerger("demo.cs")
    result = m.merge(fr("class Demo\n{\n}"), Meta(file_name="Demo.cs"))
    assert result.event != "blocked"
    assert "file_mismatch" not in result.notes


def test_allman_csharp_entities():
    cs = "class A\n{\n    void F()\n    {\n    }\n}\nclass B\n{\n}"
    assert [k for k, _, _ in entities(cs.split("\n"))] == ["class A", "class B"]


def test_injection_plan_skips_existing_code():
    master = V1
    target = "public class Main {\n\n    public static void main(String[] args) {\n        // my own note\n"
    steps, conflicts = plan_injection(master, target)
    out = apply_plan(target, steps)
    assert out.count("public class Main") == 1 and "// my own note" in out
    assert plan_injection(master, master) == ([], [])
    s2, c2 = plan_injection(master, out)
    assert not s2                          # second run adds nothing: no repeats


def test_injection_plan_empty_target_and_conflict():
    steps, _ = plan_injection("a;\nb;", "")
    assert apply_plan("", steps).strip() == "a;\nb;"
    steps, conf = plan_injection("int x = 1;\nint y = 2;", "int x = 1;\nint y = 99;")
    assert conf and not steps


def test_scrolled_fragment_not_a_new_entity():
    m = DocMerger()
    feed(m, V1)
    mid = "\n".join(V1.split("\n")[8:12])     # hello() alone, class braces off screen
    feed(m, mid, 5)
    assert sub(m) == want(V1) and m.text.count("static void hello") == 1


def test_breaker_stops_flapping():
    m = DocMerger(cfg=Cfg(req_delete=1, req_modify=1))
    base = "\n".join(f"int v{i} = {i};" for i in range(8))
    feed(m, base, 2)
    notes = []
    for i in range(12):
        body = "\n".join(f"int v{j} = {j};" for j in range(8) if j != i % 7 + 0)
        notes += m.merge(fr(body), Meta(top=True, bottom=True)).notes
        m.merge(fr(base), Meta(top=True, bottom=True))
    assert "breaker_tripped" in notes


def test_live_mode_reports_edit_as_review_without_touching_master():
    base = "class A\n{\n    int x = 1;\n}"
    edit = base.replace("int x = 1;", "int x = 2;")
    m = DocMerger("A.cs", Cfg(req_rewrite=2))   # matches the live DocCfg
    m.seed_baseline(base)

    first = m.merge(fr(edit), Meta(allow_destructive=False))
    assert first.reviews == [] and m.text == base

    second = m.merge(fr(edit), Meta(allow_destructive=False))
    assert second.event == "review"
    assert second.review_code == edit
    assert m.text == base                       # never auto-replaces
    assert len(second.reviews) == 1
    assert second.reviews[0].kind == "replacement"
    assert any("int x = 1;" in line for line in second.reviews[0].old)
    assert any("int x = 2;" in line for line in second.reviews[0].new)

    # A confirmed review must stay visible on every following frame.
    third = m.merge(fr(edit), Meta(allow_destructive=False))
    assert third.event == "review" and third.review_code == edit


def test_live_mode_reports_loose_line_edit_only_after_persistence():
    base = "int a = 1;\nint b = 2;\nint c = 3;"
    edit = "int a = 1;\nint b = 9;\nint c = 3;"
    m = DocMerger()
    m.seed_baseline(base)
    meta = Meta(allow_destructive=False, top=True, bottom=True)

    first = m.merge(fr(edit), meta)
    assert first.reviews == []
    second = m.merge(fr(edit), meta)
    assert second.review_code == edit
    assert m.text == base


def test_live_mode_never_proposes_edits_from_unreliable_frames():
    base = "class A\n{\n    int x = 1;\n}"
    edit = base.replace("int x = 1;", "int x = 2;")
    m = DocMerger("A.cs")
    m.seed_baseline(base)
    meta = Meta(allow_destructive=False, flags=("folded",))

    result = None
    for _ in range(4):
        result = m.merge(fr(edit), meta)
    assert result.reviews == [] and result.review_code == ""
    assert "destructive_skipped" in result.notes
    assert m.text == base


def test_plan_review_changes_preserves_local_edits_and_refuses_conflicts():
    base = "class A\n{\n    int x = 1;\n    int y = 2;\n}"
    review = base.replace("int x = 1;", "int x = 9;")
    target = base.replace("int y = 2;", "int y = 2;\n    // local note")

    proposed, problems = plan_review_changes(base, review, target)
    assert problems == []
    assert "int x = 9;" in proposed and "// local note" in proposed

    locally_changed, problems = plan_review_changes(
        base, review, base.replace("int x = 1;", "int x = 5;")
    )
    assert locally_changed is None and problems

    assert review_state_present(base, review, review + "\n// local note")
    assert not review_state_present(base, review, base)
    assert not review_state_present(base, review, review + "\nint x = 1;")


def test_fuzz_small_docs_converge_no_duplicates():
    rnd = random.Random(7)
    for s in range(60):
        m = DocMerger()
        body = [f"int v{i} = {i};" for i in range(rnd.randint(1, 4))]
        for step in range(15):
            op = rnd.choice(["add", "del", "mod", "rewrite", "method"])
            if op == "add":
                body.insert(rnd.randint(0, len(body)), f"int n{rnd.randint(0, 999)} = {rnd.randint(0, 9)};")
            elif op == "del" and len(body) > 1:
                body.pop(rnd.randrange(len(body)))
            elif op == "mod":
                body[rnd.randrange(len(body))] = f"int m{rnd.randint(0, 999)} = {rnd.randint(0, 9)};"
            elif op == "rewrite":
                body = [f"int r{rnd.randint(0, 999)} = {i};" for i in range(rnd.randint(1, 5))]
            else:
                body.append(f"call{rnd.randint(0, 99)}();")
            doc = "public class Main {\n    public static void main(String[] a) {\n" + \
                  "\n".join("        " + b for b in body) + "\n    }\n}"
            feed(m, doc, 3, Meta(top=True, bottom=True))
            assert sub(m) == want(doc), (s, step, op)
            assert m.text.count("public class Main") == 1
