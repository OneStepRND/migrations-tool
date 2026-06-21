import tempfile
from pathlib import Path

import pytest
import sqlalchemy as sa

from migrations_tool.migration import (
    GENESIS_HASH,
    MigrationTool,
    SchemaHash,
    compute_chain_hash,
    compute_content_hash,
    normalize_content,
)


def test_schema_hash_table_has_clickhouse_engine():
    """schema_hash must declare a ReplacingMergeTree engine (ordered by id) so it
    can be created and replaced on ClickHouse like migration_history."""
    from clickhouse_connect.cc_sqlalchemy.ddl.tableengine import ReplacingMergeTree

    engines = [
        arg for arg in SchemaHash.__table_args__ if isinstance(arg, ReplacingMergeTree)
    ]
    assert len(engines) == 1
    assert "ReplacingMergeTree" in engines[0].compile()
    assert "ORDER BY id" in engines[0].compile()


UPGRADE_TMPL = """
import sqlalchemy as sa
def upgrade(session):
    session.execute(sa.text("CREATE TABLE {table} (id INTEGER)"))
def downgrade(session):
    session.execute(sa.text("DROP TABLE {table}"))
"""


@pytest.fixture
def temp_migrations_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        dir_path = Path(tmpdir)
        (dir_path / ".gitkeep").touch()
        yield dir_path


@pytest.fixture
def tool(temp_migrations_dir: Path):
    return MigrationTool(
        database_url="sqlite:///:memory:",
        migrations_dir=str(temp_migrations_dir),
        database_echo=False,
        should_create_history_table=True,
    )


def write_migration(d: Path, filename: str, table: str) -> str:
    (d / filename).write_text(UPGRADE_TMPL.format(table=table))
    return filename


def apply_all(tool: MigrationTool):
    list(tool.lazy_execute(tool.list_migrations_to_upgrade(), upgrade=True))


# --- pure helpers -----------------------------------------------------------


def test_normalize_content_line_endings():
    assert normalize_content(b"a\r\nb\rc\n") == b"a\nb\nc\n"


def test_content_hash_ignores_line_endings():
    assert compute_content_hash(b"line1\r\nline2") == compute_content_hash(
        b"line1\nline2"
    )


def test_chain_hash_is_order_sensitive():
    a = compute_chain_hash(GENESIS_HASH, "a.py", "h1")
    ab = compute_chain_hash(a, "b.py", "h2")
    b = compute_chain_hash(GENESIS_HASH, "b.py", "h2")
    ba = compute_chain_hash(b, "a.py", "h1")
    assert ab != ba


# --- tip lifecycle ----------------------------------------------------------


def test_empty_db_tip_is_genesis(tool: MigrationTool):
    assert tool.get_schema_hash() == GENESIS_HASH
    assert tool.is_up_to_date()


def test_tip_changes_on_upgrade(temp_migrations_dir: Path, tool: MigrationTool):
    write_migration(temp_migrations_dir, "20240101_0000_000001__a.py", "t_a")
    apply_all(tool)
    assert tool.get_schema_hash() != GENESIS_HASH
    assert tool.is_up_to_date()


def test_stored_tip_matches_expected(temp_migrations_dir: Path, tool: MigrationTool):
    write_migration(temp_migrations_dir, "20240101_0000_000001__a.py", "t_a")
    write_migration(temp_migrations_dir, "20240101_0000_000002__b.py", "t_b")
    apply_all(tool)
    assert tool.get_schema_hash() == tool.compute_expected_hash()


def test_pending_migration_makes_not_up_to_date(
    temp_migrations_dir: Path, tool: MigrationTool
):
    write_migration(temp_migrations_dir, "20240101_0000_000001__a.py", "t_a")
    apply_all(tool)
    # add a new, unapplied migration file
    write_migration(temp_migrations_dir, "20240101_0000_000002__b.py", "t_b")
    assert not tool.is_up_to_date()
    assert tool.get_schema_hash() != tool.compute_expected_hash()


def test_downgrade_restores_previous_tip(
    temp_migrations_dir: Path, tool: MigrationTool
):
    write_migration(temp_migrations_dir, "20240101_0000_000001__a.py", "t_a")
    apply_all(tool)
    tip_after_a = tool.get_schema_hash()

    write_migration(temp_migrations_dir, "20240101_0000_000002__b.py", "t_b")
    apply_all(tool)
    assert tool.get_schema_hash() != tip_after_a

    # downgrade b
    list(
        tool.lazy_execute(
            tool.list_migrations_to_downgrade(
                target_filename="20240101_0000_000001__a.py"
            ),
            upgrade=False,
        )
    )
    assert tool.get_schema_hash() == tip_after_a


def test_full_downgrade_returns_to_genesis(
    temp_migrations_dir: Path, tool: MigrationTool
):
    write_migration(temp_migrations_dir, "20240101_0000_000001__a.py", "t_a")
    apply_all(tool)
    list(
        tool.lazy_execute(
            tool.list_migrations_to_downgrade(target_filename=None),
            upgrade=False,
        )
    )
    assert tool.get_schema_hash() == GENESIS_HASH


def test_tip_independent_of_application_order_with_holes(
    temp_migrations_dir: Path, tool: MigrationTool
):
    """A migration inserted 'behind' an already-applied one (a hole) must yield
    the same tip a fresh checkout would compute from the sorted files."""
    write_migration(temp_migrations_dir, "20240101_0000_000001__a.py", "t_a")
    write_migration(temp_migrations_dir, "20240101_0000_000003__c.py", "t_c")
    apply_all(tool)

    # now a 'b' migration appears with a timestamp between a and c
    write_migration(temp_migrations_dir, "20240101_0000_000002__b.py", "t_b")
    apply_all(tool)

    assert tool.get_schema_hash() == tool.compute_expected_hash()
    assert tool.is_up_to_date()


# --- drift ------------------------------------------------------------------


def test_find_drift_detects_modified_file(
    temp_migrations_dir: Path, tool: MigrationTool
):
    fn = write_migration(temp_migrations_dir, "20240101_0000_000001__a.py", "t_a")
    apply_all(tool)
    assert tool.find_drift() == []

    # modify the already-applied file's content
    (temp_migrations_dir / fn).write_text(
        UPGRADE_TMPL.format(table="t_a") + "\n# changed\n"
    )
    assert tool.find_drift() == [fn]


def test_find_drift_detects_missing_file(
    temp_migrations_dir: Path, tool: MigrationTool
):
    fn = write_migration(temp_migrations_dir, "20240101_0000_000001__a.py", "t_a")
    apply_all(tool)
    (temp_migrations_dir / fn).unlink()
    assert tool.find_drift() == [fn]


# --- self-migration of meta schema -----------------------------------------


def test_backfill_on_legacy_db(temp_migrations_dir: Path, tmp_path: Path):
    """A DB initialized before the hashing feature (no content_hash column, no
    schema_hash table) must be migrated and backfilled on next construction."""
    url = f"sqlite:///{tmp_path / 'legacy.db'}"
    fn = write_migration(temp_migrations_dir, "20240101_0000_000001__a.py", "t_a")

    # Simulate a legacy DB: only a migration_history table with the old shape,
    # holding an applied row with no content_hash, and no schema_hash table.
    engine = sa.create_engine(url)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE migration_history ("
                "filename VARCHAR(256) PRIMARY KEY, "
                "executed_at DATETIME NOT NULL)"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO migration_history (filename, executed_at) "
                "VALUES (:f, '2024-01-01 00:00:00')"
            ),
            {"f": fn},
        )
    engine.dispose()

    # Constructing the tool should self-migrate: add column, create table, seed.
    tool = MigrationTool(
        database_url=url,
        migrations_dir=str(temp_migrations_dir),
        database_echo=False,
    )

    # content_hash backfilled, schema_hash table populated, and consistent.
    with tool.Session.begin() as session:
        row = session.get(SchemaHash, 1)
        assert row is not None
        ch = session.execute(
            sa.text("SELECT content_hash FROM migration_history WHERE filename = :f"),
            {"f": fn},
        ).scalar()
    assert ch == compute_content_hash((temp_migrations_dir / fn).read_bytes())
    assert tool.get_schema_hash() == tool.compute_expected_hash()
    assert tool.is_up_to_date()
