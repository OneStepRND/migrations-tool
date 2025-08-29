import tempfile
import sqlalchemy as sa
import pytest
from pathlib import Path
from sqlalchemy.exc import SQLAlchemyError
from typer.testing import CliRunner
from migrations_tool.cli import app
from migrations_tool.migration import MigrationTool


@pytest.fixture
def temp_migrations_dir():
    """Create a temporary directory for migrations"""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def sqlite_db_url(tmp_path: Path):
    db_path = tmp_path / "test.db"
    url = f"sqlite:///{db_path}"
    yield url


@pytest.fixture()
def tool(sqlite_db_url: str, temp_migrations_dir: Path):
    tool = MigrationTool(
        database_url=sqlite_db_url,
        migrations_dir=str(temp_migrations_dir),
        database_echo=False,
        should_create_history_table=True,
    )
    return tool


@pytest.fixture
def runner():
    return CliRunner()


class TestMigrationCommands:
    """Test suite for migration CLI commands."""

    @pytest.fixture(autouse=True)
    def setup(
        self,
        runner: CliRunner,
        tool: MigrationTool,
        temp_migrations_dir: Path,
        sqlite_db_url: str,
    ):
        """Store fixtures as instance attributes for all tests."""
        self.runner = runner
        self.tool = tool
        self.temp_migrations_dir = temp_migrations_dir
        self.sqlite_db_url = sqlite_db_url

        # Common setup: ensure clean state
        assert len(self.tool.list_migrations_to_upgrade()) == 0

    def invoke_command(self, command: list[str]):
        """Helper method to invoke CLI commands with common arguments."""
        full_command = command + [
            "--database-url",
            self.sqlite_db_url,
            "--migrations-dir",
            str(self.temp_migrations_dir),
        ]
        return self.runner.invoke(app, full_command)

    def generate_migration(self, name: str = "test migration"):
        """Helper to generate a migration."""
        result = self.invoke_command(["generate", name])
        assert result.exit_code == 0, result.output
        return result

    def get_migration_filenames(self):
        """Helper to get list of migration filenames."""
        return self.tool.list_existing_migration_files()

    def add_upgrade_content(self, filename: str, content: str = "pass"):
        """Helper to add upgrade function content to a migration."""
        filepath = self.temp_migrations_dir / filename
        text = filepath.read_text()
        text = text.replace(
            "def upgrade(session: Session):\n        pass",
            f"def upgrade(session: Session):\n        {content}",
        )
        filepath.write_text(text)

    def add_downgrade_content(self, filename: str, content: str = "pass"):
        """Helper to add downgrade function content to a migration."""
        filepath = self.temp_migrations_dir / filename
        text = filepath.read_text()
        text = text.replace(
            "def downgrade(session: Session):\n        pass",
            f"def downgrade(session: Session):\n        {content}",
        )
        filepath.write_text(text)

    def test_generate_command_basic(self):
        """Test basic migration generation."""
        # Act
        _ = self.generate_migration()

        # Assert
        assert len(self.tool.list_migrations_to_upgrade()) == 1

    def test_generate_command_with_spaces_in_description(self):
        """Test generation with spaces in description."""
        # Act
        result = self.generate_migration("my complex migration name")

        # Assert
        assert result.exit_code == 0
        filenames = self.get_migration_filenames()
        assert len(filenames) == 1
        assert "my_complex_migration_name" in filenames[0]

    def test_upgrade_command_basic(self):
        """Test basic migration upgrade."""
        # Arrange: Generate a migration first
        self.generate_migration()
        assert len(self.tool.list_migrations_to_upgrade()) == 1

        # Act: Run upgrade
        result = self.invoke_command(["upgrade"])

        # Assert
        assert result.exit_code == 0, result.output
        assert len(self.tool.list_migrations_to_upgrade()) == 0
        assert len(self.tool.list_applied_migration_files()) == 1

    def test_upgrade_command_no_pending_migrations(self):
        """Test upgrade when no migrations are pending."""
        # Act: Run upgrade with no pending migrations
        result = self.invoke_command(["upgrade"])

        # Assert
        assert result.exit_code == 0
        assert "no migrations to apply" in result.output

    def test_multiple_migrations_upgrade(self):
        """Test upgrading multiple migrations."""
        # Arrange: Generate multiple migrations
        self.generate_migration("first migration")
        self.generate_migration("second migration")
        self.generate_migration("third migration")
        assert len(self.tool.list_migrations_to_upgrade()) == 3

        # Act: Upgrade all
        result = self.invoke_command(["upgrade"])

        # Assert
        assert result.exit_code == 0, result.output
        assert len(self.tool.list_migrations_to_upgrade()) == 0
        assert len(self.tool.list_applied_migration_files()) == 3

    def test_upgrade_with_target_file(self):
        """Test upgrading to a specific target migration."""
        # Arrange: Generate multiple migrations
        self.generate_migration("first")
        self.generate_migration("second")
        self.generate_migration("third")
        filenames = self.get_migration_filenames()
        target_file = filenames[1]  # Target the second migration

        # Act: Upgrade to specific target
        result = self.invoke_command(["upgrade", "--target-file", target_file])

        # Assert
        assert result.exit_code == 0
        assert len(self.tool.list_applied_migration_files()) == 2
        assert len(self.tool.list_migrations_to_upgrade()) == 1

    def test_downgrade_command_basic(self):
        """Test basic downgrade functionality."""
        # Arrange: Generate and apply migrations
        self.generate_migration("first")
        self.generate_migration("second")
        self.invoke_command(["upgrade"])
        filenames = self.get_migration_filenames()
        target_file = filenames[0]  # Downgrade to first migration

        # Act: Downgrade with confirmation
        result = self.invoke_command(["downgrade", "--filename", target_file, "-y"])

        # Assert
        assert result.exit_code == 0
        assert len(self.tool.list_applied_migration_files()) == 1
        assert self.tool.list_applied_migration_files()[0] == target_file

    def test_downgrade_command_cancelled(self):
        """Test downgrade cancelled by user."""
        # Arrange: Generate and apply migrations
        self.generate_migration("first")
        self.generate_migration("second")
        self.invoke_command(["upgrade"])
        filenames = self.get_migration_filenames()

        # Act: Downgrade but cancel confirmation
        result = self.invoke_command(["downgrade", filenames[0]])

        # Assert
        assert result.exit_code == 2
        assert len(self.tool.list_applied_migration_files()) == 2  # No change

    def test_downgrade_to_nonexistent_file(self):
        """Test downgrade with invalid target file."""
        # Arrange: Generate and apply a migration
        self.generate_migration()
        self.invoke_command(["upgrade"])

        # Act: Try to downgrade to non-existent file
        result = self.invoke_command(["downgrade", "nonexistent.py"])

        # Assert
        assert result.exit_code != 0

    def test_list_command_empty(self):
        """Test list command with no migrations."""
        # Act
        result = self.invoke_command(["list"])

        # Assert
        assert result.exit_code == 0

    def test_list_command_with_migrations(self):
        """Test list command with migrations."""
        # Arrange: Generate migrations and apply some
        self.generate_migration("first")
        self.generate_migration("second")
        self.generate_migration("third")
        # Apply only first two
        filenames = self.get_migration_filenames()
        self.invoke_command(["upgrade", "--target-file", filenames[1]])

        # Act
        result = self.invoke_command(["list"])

        # Assert
        assert result.exit_code == 0
        assert "applied" in result.output
        assert "pending" in result.output

    def test_list_command_with_limit(self):
        """Test list command with limit option."""
        # Arrange: Generate and apply multiple migrations
        for i in range(5):
            self.generate_migration(f"migration_{i}")
        self.invoke_command(["upgrade"])

        # Act
        result = self.invoke_command(["list", "--limit", "3"])

        # Assert
        assert result.exit_code == 0

    def test_current_command_no_migrations(self):
        """Test current command with no applied migrations."""
        # Act
        result = self.invoke_command(["current"])

        # Assert
        assert result.exit_code == 0
        assert result.output.strip() == ""

    def test_current_command_with_migrations(self):
        """Test current command with applied migrations."""
        # Arrange: Generate and apply migrations
        self.generate_migration("first")
        self.generate_migration("second")
        self.invoke_command(["upgrade"])
        filenames = self.get_migration_filenames()

        # Act
        result = self.invoke_command(["current"])

        # Assert
        assert result.exit_code == 0
        assert filenames[-1] in result.output

    def test_pending_command_no_migrations(self):
        """Test pending command with no pending migrations."""
        # Act
        result = self.invoke_command(["pending"])

        # Assert
        assert result.exit_code == 0
        assert result.output.strip() == ""

    def test_pending_command_with_migrations(self):
        """Test pending command with pending migrations."""
        # Arrange: Generate migrations but don't apply
        self.generate_migration("first")
        self.generate_migration("second")
        filenames = self.get_migration_filenames()

        # Act
        result = self.invoke_command(["pending"])

        # Assert
        assert result.exit_code == 0
        lines = result.output.strip().split("\n")
        assert len(lines) == 2
        assert filenames[0] in result.output
        assert filenames[1] in result.output

    def test_init_command(self):
        """Test init command creates history table."""
        # Note: History table already exists from setup, but init should be idempotent
        # Act
        result = self.invoke_command(["init"])

        # Assert
        assert result.exit_code == 1, f"init should only run once : {result.output}"

    def test_verbose_flag(self):
        """Test verbose logging flag."""
        # Act
        result = self.invoke_command(["--verbose", "list"])

        # Assert
        assert result.exit_code == 0
        # Verbose should show debug logs with specific format
        assert "DEBUG" in result.output or "D " in result.output

    def test_partial_upgrade_then_full_upgrade(self):
        """Test partial upgrade followed by full upgrade."""
        # Arrange: Generate multiple migrations
        self.generate_migration("first")
        self.generate_migration("second")
        self.generate_migration("third")
        filenames = self.get_migration_filenames()

        # Act 1: Partial upgrade
        result1 = self.invoke_command(["upgrade", "--target-file", filenames[0]])
        assert result1.exit_code == 0
        assert len(self.tool.list_applied_migration_files()) == 1

        # Act 2: Full upgrade
        result2 = self.invoke_command(["upgrade"])
        assert result2.exit_code == 0
        assert len(self.tool.list_applied_migration_files()) == 3

    def test_migration_with_actual_sql(self):
        """Test migrations that execute actual SQL."""
        # Arrange: Use tool to generate migration and get filepath
        first = self.tool.generate_migration("first_migration")
        filepath = self.tool.generate_migration("create_table")

        # Write complete migration file with actual SQL operations
        filepath.write_text("""
import sqlalchemy as sa
from sqlalchemy.orm import Session

def upgrade(session: Session):
    session.execute(sa.text("CREATE TABLE test_table (id INTEGER PRIMARY KEY)"))

def downgrade(session: Session):
    session.execute(sa.text("DROP TABLE test_table"))
""")
        with pytest.raises(SQLAlchemyError):
            with self.tool.Session.begin() as session:
                assert (
                    session.execute(sa.text("select * from test_table;")).first()
                    is None
                )
        # Act: Upgrade
        result = self.invoke_command(["upgrade"])

        # Assert
        assert result.exit_code == 0
        assert len(self.tool.list_applied_migration_files()) == 2

        with self.tool.Session.begin() as session:
            assert session.execute(sa.text("select * from test_table;")).first() is None

        # Act: Downgrade
        result = self.invoke_command(["downgrade", "--filename", first.name, "-y"])

        # Assert
        assert result.exit_code == 0
        assert len(self.tool.list_applied_migration_files()) == 1

    def test_downgrade_no_args(self):
        """Test complex sequence of operations."""
        # Generate migrations
        self.generate_migration("m1")
        self.generate_migration("m2")
        self.generate_migration("m3")
        self.generate_migration("m4")
        self.generate_migration("m5")
        self.invoke_command(["upgrade"])
        applied = self.tool.list_applied_migration_files()
        assert len(applied) == 5
        result = self.invoke_command(["downgrade", "-y"])
        assert result.exit_code == 0, result.output

        after_downgrade = self.tool.list_applied_migration_files()
        assert len(after_downgrade) == 4
        applied.pop()
        assert applied == after_downgrade

        result = self.invoke_command(["downgrade", "-y", "-n", "3"])
        assert result.exit_code == 0, result.output
        after_downgrade = self.tool.list_applied_migration_files()
        assert after_downgrade == [self.tool.list_applied_migration_files()[0]]
