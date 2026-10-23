"""Cross-version matcher: each pass, plus the cases where it deliberately refuses to match.

The real v1 -> v2 change only exercises the exact pass, so these synthetic versions cover
renumbering, renaming, moves, and ambiguity.
"""
from app.parsing.normalize import content_hash
from app.versioning.matcher import NodeRef, match_versions

LONG = "The device automatically powers off after 60 seconds of inactivity on the home screen."


def ref(key, title, parent="doc", body="", number=None):
    number = number if number is not None else (key if key != "doc" else None)
    return NodeRef(key, number, title, parent if key != "doc" else None, content_hash(body), body)


def base():
    return [
        ref("doc", "Manual"),
        ref("3", "Device Operation"),
        ref("3.3", "Auto Shutoff", "3", LONG),
        ref("3.4", "Result Display", "3", "Shows systolic, diastolic and pulse readings together with a class."),
        ref("4", "Alarms"),
        ref("4.2", "Error Codes", "4", "E1 cuff not connected. E3 overpressure auto-deflates within 2 seconds."),
        ref("7", "Troubleshooting"),
        ref("7.1", "Error Codes", "7", "If a code from Section 4.2 persists, contact CardioTrack support."),
    ]


def test_identical_versions_match_exactly_and_unchanged():
    r = match_versions(base(), base())
    assert all(m.method in ("root", "exact") and m.status == "unchanged" for m in r.matches.values())
    assert r.removed == [] and r.notes == []


def test_renumbered_sections_keep_identity_via_title_under_parent():
    new = base()
    new[2] = ref("3.4", "Auto Shutoff", "3", LONG)                       # was 3.3
    new[3] = ref("3.3", "Result Display", "3", base()[3].body)           # was 3.4
    r = match_versions(base(), new)
    m = r.matches["3.4"]
    assert m.old_key == "3.3" and m.method == "title_parent"
    assert m.status == "unchanged" and m.moved and not m.title_changed
    assert r.matches["3.3"].old_key == "3.4"
    assert r.removed == []


def test_duplicate_titles_are_matched_through_their_own_parents():
    new = base()
    new[5] = ref("4.3", "Error Codes", "4", base()[5].body)              # 4.2 -> 4.3
    new[7] = ref("7.2", "Error Codes", "7", base()[7].body)              # 7.1 -> 7.2
    r = match_versions(base(), new)
    assert r.matches["4.3"].old_key == "4.2"
    assert r.matches["7.2"].old_key == "7.1"                             # not swapped


def test_ambiguous_same_title_siblings_are_not_guessed():
    old = [ref("doc", "M"), ref("1", "Safety"),
           ref("1.1", "Warnings", "1", "Keep away from water at all times when in use."),
           ref("1.2", "Warnings", "1", "Do not use on infants or on an arm with an IV line.")]
    new = [ref("doc", "M"), ref("1", "Safety"),
           ref("1.5", "Warnings", "1", "Keep away from water at all times when in use, edited."),
           ref("1.6", "Warnings", "1", "Never use on infants, or on an arm with an IV line.")]
    r = match_versions(old, new)
    # title_parent refuses (2 old vs 2 new "Warnings"); body similarity then decides each alone
    assert any("ambiguous title 'warnings'" in n for n in r.notes)
    assert (r.matches["1.5"].old_key, r.matches["1.5"].method) == ("1.1", "body~")
    # 1.6 was reworded below BODY_SIMILARITY: lineage is lost, but visibly (added + removed),
    # never attached to the wrong predecessor. Documented failure mode.
    assert r.matches["1.6"].status == "added"
    assert r.removed == ["1.2"]


def test_reworded_heading_with_same_number_is_matched_and_flagged():
    new = base()
    new[2] = ref("3.3", "Automatic Shutoff", "3", LONG)
    m = match_versions(base(), new).matches["3.3"]
    assert m.method == "number_title~" and m.old_key == "3.3"
    assert m.title_changed and m.status == "unchanged"


def test_moved_and_renamed_section_with_identical_text_matches_by_body():
    new = base()
    del new[2]
    new.append(ref("4.9", "Power Saving", "4", LONG))
    m = match_versions(base(), new).matches["4.9"]
    assert m.method == "same_body" and m.old_key == "3.3" and m.moved and m.title_changed


def test_moved_renamed_and_lightly_edited_section_matches_by_similar_body():
    new = base()
    del new[2]
    new.append(ref("4.9", "Power Saving", "4", LONG.replace("60 seconds", "90 seconds")))
    m = match_versions(base(), new).matches["4.9"]
    assert m.method == "body~" and m.old_key == "3.3" and m.status == "modified"


def test_empty_body_headings_are_never_matched_by_hash():
    # every empty chapter heading hashes the same; a renamed, renumbered one must NOT match
    old = base() + [ref("9", "Appendix")]
    new = base() + [ref("10", "Glossary")]
    r = match_versions(old, new)
    assert r.matches["10"].status == "added"
    assert "9" in r.removed


def test_deleted_and_new_sections():
    new = [n for n in base() if n.key != "7.1"] + [ref("5.3", "Data Export", "doc", "CSV export since firmware 1.4.")]
    r = match_versions(base(), new)
    assert r.removed == ["7.1"]
    assert r.matches["5.3"].status == "added"
