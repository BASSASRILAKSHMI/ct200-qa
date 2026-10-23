"""Ingesting v1 then v2 into a fresh SQLite database."""
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.db.base import init_db, make_engine
from app.db.models import DocumentVersion, LogicalNode, NodeVersion
from app.services import ingestion
from app.services.ingestion import IngestionError, ingest_pdf
from app.versioning.diff import word_diff

from .conftest import V1, V2


@pytest.fixture()
def session(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    init_db(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    yield s
    s.close()
    engine.dispose()


def nv(session, version_number, number):
    return session.scalars(
        select(NodeVersion).join(DocumentVersion)
        .where(DocumentVersion.version_number == version_number, NodeVersion.number == number)
    ).one()


@pytest.fixture()
def both(session):
    ingest_pdf(session, V1)
    out = ingest_pdf(session, V2)
    return session, out


def test_v2_is_a_new_version_and_v1_is_untouched(both):
    s, out = both
    assert out.created and out.version.version_number == 2
    assert s.scalar(select(func.count()).select_from(DocumentVersion)) == 2
    v1_32 = nv(s, 1, "3.2")
    assert "40 mmHg increments" in v1_32.body                 # v1 text still exactly as ingested
    assert "30 mmHg increments" in nv(s, 2, "3.2").body


def test_expected_change_statuses(both):
    _, out = both
    s = out.summary
    assert s["counts"] == {"unchanged": 24, "modified": 4, "added": 1, "removed": 0}
    assert sorted(s["modified"]) == ["2.1.1.1", "3.2", "4.2", "4.3"]
    assert s["added"] == ["5.3"]


def test_unchanged_sections_share_one_logical_node(both):
    s, _ = both
    for number in ("1.1", "3.4", "7.1", "8.1"):
        a, b = nv(s, 1, number), nv(s, 2, number)
        assert a.id != b.id                                    # separate immutable rows
        assert a.logical_node_id == b.logical_node_id          # same logical section
        assert b.previous_id == a.id and b.change_status == "unchanged"


def test_duplicate_titles_map_to_their_own_predecessors(both):
    s, _ = both
    assert nv(s, 2, "4.2").logical_node_id == nv(s, 1, "4.2").logical_node_id
    assert nv(s, 2, "7.1").logical_node_id == nv(s, 1, "7.1").logical_node_id
    assert nv(s, 1, "4.2").logical_node_id != nv(s, 1, "7.1").logical_node_id


def test_added_section_gets_a_new_logical_node(both):
    s, out = both
    added = nv(s, 2, "5.3")
    assert added.change_status == "added" and added.previous_id is None
    assert s.get(LogicalNode, added.logical_node_id).first_version_id == out.version.id


def test_parent_links_stay_inside_their_version(both):
    s, _ = both
    for version in (1, 2):
        child = nv(s, version, "2.1.1.1")
        assert child.parent.number == "2.1"
        assert child.parent.version_id == child.version_id
        assert "missing_parent:2.1.1" in child.flags


def test_reingesting_identical_file_creates_nothing(both):
    s, _ = both
    again = ingest_pdf(s, V2)
    assert not again.created and again.version.version_number == 2
    assert s.scalar(select(func.count()).select_from(DocumentVersion)) == 2


def test_reverting_to_v1_is_a_real_new_version(both):
    s, _ = both
    v3 = ingest_pdf(s, V1)
    assert v3.created and v3.version.version_number == 3
    assert [r["key"] for r in v3.summary["removed"]] == ["5.3"]
    assert nv(s, 3, "3.2").logical_node_id == nv(s, 1, "3.2").logical_node_id
    assert nv(s, 3, "3.2").content_hash == nv(s, 1, "3.2").content_hash


def test_incomplete_parse_is_refused_and_nothing_written(session, monkeypatch):
    real = ingestion.parse_pdf

    def broken(path, force_ocr=False):
        r = real(path, force_ocr)
        r.duplicated_line_ids = [3]
        return r

    monkeypatch.setattr(ingestion, "parse_pdf", broken)
    with pytest.raises(IngestionError, match="coverage check failed"):
        ingest_pdf(session, V1)
    assert session.scalar(select(func.count()).select_from(DocumentVersion)) == 0


def test_word_diff_reports_threshold_changes(both):
    s, _ = both
    d = word_diff(nv(s, 1, "4.2").body, nv(s, 2, "4.2").body)
    assert d["numbers_removed"] == ["2"] and "1.5" in d["numbers_added"]
    d = word_diff(nv(s, 1, "4.3").body, nv(s, 2, "4.3").body)
    assert d["numbers_removed"] == ["E5"] and d["numbers_added"] == ["E6"]
