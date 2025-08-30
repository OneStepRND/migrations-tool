import tempfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError

from migrations_tool.migration import MigrationTool, parse_filename


@pytest.fixture
def temp_migrations_dir():
    """Create a temporary directory for migrations"""
    with tempfile.TemporaryDirectory() as tmpdir:
        dir_path = Path(tmpdir)
        _ = (dir_path / ".gitkeep").touch()
        yield dir_path


@pytest.fixture
def sqlite_db_url():
    """Create an in-memory SQLite database URL"""
    return "sqlite:///:memory:"


@pytest.fixture
def migration_tool(temp_migrations_dir: Path, sqlite_db_url: str):
    """Create a MigrationTool instance with temp directory and in-memory DB"""
    return MigrationTool(
        database_url=sqlite_db_url,
        migrations_dir=str(temp_migrations_dir),
        database_echo=False,
        should_create_history_table=True,
    )


@pytest.fixture
def sample_migration_content():
    """Sample migration file content"""
    return '''"""
Migration: test migration
Created: 2024-01-15T10:30:00+00:00
Revision ID: abc123
"""

import sqlalchemy as sa


def upgrade(session):
    """Apply migration changes"""
    session.execute(sa.text("CREATE TABLE test_table (id INTEGER PRIMARY KEY, name TEXT)"))

def downgrade(session):
    """Revert migration changes"""
    session.execute(sa.text("DROP TABLE test_table"))
'''


@pytest.fixture
def failing_migration_content():
    """Sample failing migration file content"""
    return '''"""
Migration: failing migration
Created: 2024-01-15T11:30:00+00:00
Revision ID: def456
"""

import sqlalchemy as sa


def upgrade(session):
    """Apply migration changes that will fail"""
    session.execute(sa.text("CREATE TABLE invalid_syntax_table ("))  # Invalid SQL

def downgrade(engine: sa.Engine):
    """Revert migration changes"""
    pass
'''


def test_init_creates_migration_table(migration_tool: MigrationTool):
    """Test that initialization creates the migration history table"""
    with migration_tool.engine.connect() as conn:
        # Check if migration_history table exists
        result = conn.execute(
            sa.text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='migration_history'"
            )
        )
        assert result.fetchone() is not None


def test_parse_filename_valid(migration_tool: MigrationTool):
    """Test parsing valid migration filename"""
    filename = "20250721_1337_18006715__foo.py"
    created_at, description = parse_filename(filename)

    assert description == "foo"
    assert created_at.year == 2025
    assert created_at.month == 7
    assert created_at.day == 21


def test_parse_filename_invalid(migration_tool: MigrationTool):
    """Test parsing invalid migration filenames"""
    with pytest.raises(ValueError):
        parse_filename("invalid_format.py")

    with pytest.raises(ValueError):
        parse_filename("20240115_abc123_test.py")  # Invalid time format


def test_discover_migrations_empty(migration_tool: MigrationTool):
    """Test discovering migrations in empty directory"""
    migrations = migration_tool.list_existing_migration_files()
    assert len(migrations) == 0


def test_discover_migrations_with_files(
    migration_tool: MigrationTool,
    temp_migrations_dir: Path,
    sample_migration_content: str,
):
    """Test discovering migrations with valid files"""
    # Create a valid migration file
    filename = "20250721_1337_18006715__foo2.py"
    migration_file = temp_migrations_dir / filename
    migration_file.write_text(sample_migration_content)

    migrations = migration_tool.list_existing_migration_files()
    assert migrations == [filename]


def test_generate_migration(migration_tool: MigrationTool):
    """Test generating a new migration"""
    with patch("migrations_tool.migration.datetime") as mock_datetime:
        mock_datetime.now.return_value = datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC)
        mock_datetime.isoformat = datetime.isoformat
        filename = migration_tool.generate_migration("test migration")
        # Check that file was created
        assert filename.is_file()
        assert filename.exists()


def test_upgrade_no_migrations(migration_tool: MigrationTool):
    """Test upgrade when no migrations exist"""
    # Should not raise any error
    migration_tool.lazy_execute(
        migration_tool.list_migrations_to_upgrade(),
        upgrade=True,
    )


def test_upgrade_single_migration(
    migration_tool: MigrationTool,
    temp_migrations_dir: Path,
    sample_migration_content: str,
):
    """Test upgrading with a single migration"""
    # Create migration file
    filename = "20240115_103000_abc123_test_migration.py"
    migration_file = temp_migrations_dir / filename
    migration_file.write_text(sample_migration_content)

    # Run upgrade
    _ = list(
        migration_tool.lazy_execute(
            migration_tool.list_migrations_to_upgrade(), upgrade=True
        )
    )

    # Check that table was created
    with migration_tool.engine.connect() as conn:
        result = conn.execute(
            sa.text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='test_table'"
            )
        )
        assert result.fetchone() is not None

    # Check migration was recorded
    assert (
        "20240115_103000_abc123_test_migration.py"
        in migration_tool.list_applied_migration_files()
    )


def test_upgrade_multiple_migrations(
    migration_tool: MigrationTool,
    temp_migrations_dir: Path,
):
    """Test upgrading multiple migrations in order"""
    # Create multiple migration files
    migration1_content = """
import sqlalchemy as sa
def upgrade(session):
    session.execute(sa.text("CREATE TABLE table1 (id INTEGER)"))

def downgrade(session):
    session.execute(sa.text("DROP TABLE table1"))

"""

    migration2_content = """
import sqlalchemy as sa
def upgrade(session):
    session.execute(sa.text("CREATE TABLE table2 (id INTEGER)"))
def downgrade(session):
    session.execute(sa.text("DROP TABLE table2"))
"""

    file1 = temp_migrations_dir / "20240115_100000_aaa111_first.py"
    file2 = temp_migrations_dir / "20240115_110000_bbb222_second.py"
    file1.write_text(migration1_content)
    file2.write_text(migration2_content)
    _ = list(
        migration_tool.lazy_execute(
            migration_tool.list_migrations_to_upgrade(), upgrade=True
        )
    )

    # Check both tables exist
    with migration_tool.engine.connect() as conn:
        for table in ["table1", "table2"]:
            result = conn.execute(
                sa.text(
                    f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'"
                )
            )
            assert result.fetchone() is not None


def test_upgrade_target_revision(
    migration_tool: MigrationTool,
    temp_migrations_dir: Path,
):
    """Test upgrade to specific target revision"""
    # Create two migrations
    migration1_content = """
import sqlalchemy as sa
def upgrade(session):
    session.execute(sa.text("CREATE TABLE target_table1 (id INTEGER)"))
def downgrade(engine): pass
"""

    migration2_content = """
import sqlalchemy as sa
def upgrade(session):
    session.execute(sa.text("CREATE TABLE target_table2 (id INTEGER)"))
def downgrade(engine): pass
"""
    filename1 = "20240115_100000_target1_first.py"
    filename2 = "20240115_110000_target2_second.py"
    file1 = temp_migrations_dir / filename1
    file2 = temp_migrations_dir / filename2
    file1.write_text(migration1_content)
    file2.write_text(migration2_content)

    # Run only up to first migration
    list(
        migration_tool.lazy_execute(
            migration_tool.list_migrations_to_upgrade(target_filename=filename1),
            upgrade=True,
        )
    )

    # Check only first table exists
    with migration_tool.engine.connect() as conn:
        result = conn.execute(
            sa.text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='target_table1'"
            )
        )
        assert result.fetchone() is not None

        result = conn.execute(
            sa.text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='target_table2'"
            )
        )
        assert result.fetchone() is None


def test_upgrade_failing_migration(
    migration_tool: MigrationTool,
    temp_migrations_dir: Path,
    failing_migration_content: str,
):
    """Test upgrade with a failing migration"""
    # Create failing migration
    filename = "20240115_113000_def456_failing.py"
    migration_file = temp_migrations_dir / filename
    migration_file.write_text(failing_migration_content)

    # Should raise exception
    with pytest.raises(SQLAlchemyError):
        files = migration_tool.list_migrations_to_upgrade()
        assert files
        list(migration_tool.lazy_execute(files, upgrade=True))

    # Check migration was recorded as failed
    applied = migration_tool.list_applied_migration_files()
    assert filename not in applied


def test_run_specific_migration(
    migration_tool: MigrationTool,
    temp_migrations_dir: Path,
    sample_migration_content: str,
):
    """Test running a specific migration"""
    # Create migration file
    filename = "20240115_103000_abc123_specific.py"
    migration_file = temp_migrations_dir / filename
    migration_file.write_text(sample_migration_content)

    # Run specific migration
    _ = list(migration_tool.lazy_execute([filename], upgrade=True))

    # Check migration was executed
    assert filename in migration_tool.list_applied_migration_files()


def test_run_specific_migration_not_found(migration_tool: MigrationTool):
    filename = "20240115_103000_abc123_specific.py"
    with pytest.raises(FileNotFoundError):
        _ = list(migration_tool.lazy_execute([filename], upgrade=True))


def test_downgrade_single_migration(
    migration_tool: MigrationTool,
    temp_migrations_dir: Path,
    sample_migration_content: str,
):
    """Test downgrading a single migration"""
    # Create and run migration
    filename = "20240115_103000_abc123_downgrade.py"
    migration_file = temp_migrations_dir / filename
    migration_file.write_text(sample_migration_content)
    _ = list(
        migration_tool.lazy_execute(
            migration_tool.list_migrations_to_upgrade(), upgrade=True
        )
    )

    # Verify table exists
    with migration_tool.engine.connect() as conn:
        result = conn.execute(
            sa.text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='test_table'"
            )
        )
        assert result.fetchone() is not None

    # Downgrade
    _ = list(
        migration_tool.lazy_execute(
            migration_tool.list_migrations_to_downgrade(target_filename=None),
            upgrade=False,
        )
    )

    # Verify table is removed
    with migration_tool.engine.connect() as conn:
        result = conn.execute(
            sa.text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='test_table'"
            )
        )
        assert result.fetchone() is None

    # Verify migration is removed from history
    assert filename not in migration_tool.list_applied_migration_files()


def test_migration_ordering(migration_tool: MigrationTool, temp_migrations_dir: Path):
    """Test that migrations are executed in chronological order"""
    # Create migrations with different timestamps
    migration1_content = """
import sqlalchemy as sa
def upgrade(session):
    session.execute(sa.text("CREATE TABLE order_table1 (id INTEGER)"))
def downgrade(engine): pass
"""

    migration2_content = """
import sqlalchemy as sa
def upgrade(session):
    session.execute(sa.text("ALTER TABLE order_table1 ADD COLUMN name TEXT"))
def downgrade(engine): pass
"""

    # Create files in reverse alphabetical order but correct timestamp order
    filename1 = "20240115_100000_zzz999_first.py"
    filename2 = "20240115_110000_aaa111_second.py"
    file1 = temp_migrations_dir / filename1
    file2 = temp_migrations_dir / filename2
    file1.write_text(migration1_content)
    file2.write_text(migration2_content)

    # Should execute in timestamp order, not alphabetical order
    _ = list(
        migration_tool.lazy_execute(
            migration_tool.list_migrations_to_upgrade(), upgrade=True
        )
    )

    # If executed in correct order, both operations should succeed
    assert migration_tool.list_applied_migration_files() == [filename1, filename2]
