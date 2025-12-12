import importlib.util
import logging
import textwrap
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple, Protocol, runtime_checkable

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

log = logging.getLogger(__name__)
DATETIME_FMT = "%Y%m%d_%H%M_%S%f"


class MigrationsToolError(Exception):
    pass


class Base(DeclarativeBase):
    pass


@runtime_checkable
class MigrationModule(Protocol):
    @staticmethod
    def upgrade(session: Session) -> None:
        pass

    @staticmethod
    def downgrade(session: Session) -> None:
        pass


@dataclass
class MigrationHistory(Base):
    __tablename__ = "migration_history"
    filename: Mapped[str] = mapped_column(sa.String(256), primary_key=True)
    executed_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
    )


class MigrationInfo(NamedTuple):
    filename: str
    created_at: datetime
    executed_at: datetime | None


def parse_filename(filename: str) -> tuple[datetime, str]:
    log.debug(f"Parsing filename: {filename}")
    filename = filename.split(".", maxsplit=1)[0]
    datetime_str, description = filename.split("__", maxsplit=1)

    log.debug(f"Parsed parts - date: {datetime_str} description: {description}")
    created_at = datetime.strptime(datetime_str, DATETIME_FMT)
    created_at = created_at.replace(tzinfo=UTC)
    log.debug(f"Successfully parsed timestamp: {created_at}")
    return created_at, description


def save_migration_history(filename: str, session: Session):
    log.debug(f"Recording migration execution: {filename})")
    record = MigrationHistory(
        filename=filename,
        executed_at=datetime.now(UTC),
    )
    session.add(record)
    log.debug(f"Migration execution recorded: {filename}")
    return record


def delete_migration_history(filename: str, session: Session):
    session.execute(
        sa.delete(MigrationHistory).where(MigrationHistory.filename == filename)
    )
    return filename


def load_migration_module(filepath: Path) -> MigrationModule:
    """Dynamically load migration module"""
    log.debug(f"Loading migration module from: {filepath}")
    spec = importlib.util.spec_from_file_location("migration", filepath)
    if spec is None:
        raise TypeError(f"failed to create spec for : {filepath}")
    module = importlib.util.module_from_spec(spec)
    if spec.loader is None:
        raise TypeError(f"no loader for : {filepath}")

    spec.loader.exec_module(module)
    log.debug(f"Successfully loaded module: {filepath.name}")
    if not isinstance(module, MigrationModule):
        raise TypeError(f"module found at {filepath} does not match protocol")
    return module


def create_history_table(engine: sa.Engine):
    log.debug(f"create {MigrationHistory.__tablename__}")
    Base.metadata.create_all(engine, checkfirst=False)


def assert_history_table_exists(engine: sa.Engine):
    if not sa.inspect(engine).has_table(MigrationHistory.__tablename__):
        raise MigrationsToolError(
            textwrap.dedent(f"""
database at {engine} has not being initialized
table `{MigrationHistory.__tablename__}` is missing
call init explicitly with this db before creating migrations with command:
`migrate init`""")
        )


class MigrationTool:
    def __init__(
        self,
        database_url: str,
        migrations_dir: str = "migrations",
        database_echo: bool = False,
        should_create_history_table: bool = False,
    ):
        self.engine = sa.create_engine(database_url, echo=database_echo)
        self.Session = sessionmaker(
            bind=self.engine,
            autoflush=True,
            autocommit=False,
            expire_on_commit=False,
        )
        self.migrations_dir = Path(migrations_dir)
        if not self.migrations_dir.exists():
            raise MigrationsToolError(
                f"directory `{self.migrations_dir.absolute().as_uri()}` not found"
            )
        if should_create_history_table:
            create_history_table(self.engine)
        assert_history_table_exists(self.engine)

    def load(self, filename: str):
        return load_migration_module(self.migrations_dir / filename)

    def list_existing_migration_files(self) -> list[str]:
        log.debug(f"listing migrations in: {self.migrations_dir}")
        return sorted(
            f.name
            for f in self.migrations_dir.iterdir()
            if f.is_file() and not f.name.startswith("__") and not f.name == ".gitkeep"
        )

    def list_applied_migration_files(self) -> list[str]:
        with self.Session.begin() as session:
            return sorted(
                session.execute(sa.select(MigrationHistory.filename)).scalars()
            )

    def list_migrations_to_upgrade(
        self,
        *,
        target_filename: str | None = None,
    ):
        applied_files = self.list_applied_migration_files()
        existing_files = self.list_existing_migration_files()

        # Get unapplied migrations in forward order
        migrations = [f for f in existing_files if f not in applied_files]

        if target_filename:
            if target_filename not in existing_files:
                raise ValueError(f"Target migration {target_filename} not found")
            # Include target in execution
            try:
                target_index = existing_files.index(target_filename)
            except ValueError as e:
                e.add_note(f"Target migration {target_filename} not found")
                raise e

            migrations = [
                f for f in migrations if existing_files.index(f) <= target_index
            ]
        log.debug(f"found upgrade migrations : {migrations}")
        return migrations

    def list_migrations_to_downgrade(
        self,
        *,
        target_filename: str | None = None,
    ):
        applied_files = self.list_applied_migration_files()
        existing_files = self.list_existing_migration_files()

        # Get applied migrations in reverse order

        migrations = list(reversed(applied_files))

        if target_filename:
            if target_filename not in existing_files:
                raise ValueError(
                    f"Target migration {target_filename} not found in existing_files"
                )
            try:
                target_index = migrations.index(target_filename)
            except ValueError as e:
                e.add_note(
                    f"Target migration {target_filename} not found in applied files"
                )
                raise e

            # Exclude target from execution (it becomes the new current state)
            if target_filename in migrations:
                migrations = migrations[:target_index]
        log.debug(f"found downgrade migrations : {migrations}")
        return migrations

    def lazy_execute(self, files: list[str], upgrade: bool):
        db_update = save_migration_history if upgrade else delete_migration_history
        log.debug(f"starting {upgrade=} for {len(files)}")

        for file in files:
            log.debug(f"staring to execute : {file} : {upgrade=} {db_update}")
            module = self.load(file)
            function_to_apply = module.upgrade if upgrade else module.downgrade
            yield self.apply(file, function_to_apply, db_update)
            log.debug(f"finished to execute : {file} {upgrade=} {db_update}")

    def apply(
        self,
        filename: str,
        function_to_apply: Callable[[Session], None],
        db_update: Callable[[str, Session], Any],
    ):
        with self.Session.begin() as session:
            function_to_apply(session)
            db_update(filename, session)

        return filename

    def generate_migration(self, description: str):
        """Generate a new migration file"""
        log.debug(f"Generating migration with description: {description}")
        timestamp = datetime.now(UTC)
        filename = (
            f"{timestamp.strftime(DATETIME_FMT)}__{description.replace(' ', '_')}.py"
        )
        filepath = self.migrations_dir / filename
        log.debug(f"Generated migration: {filepath}")
        filepath.write_text(
            textwrap.dedent(
                """
            import sqlalchemy as sa
            from sqlalchemy.orm import Session

            def upgrade(session: Session):
                session.execute(sa.text(''))

            def downgrade(session: Session):
                session.execute(sa.text(''))
            """
            )
        )
        return filepath

    def get_current_revision(self) -> str:
        if files := self.list_applied_migration_files():
            return files[-1]
        return ""

    def list_all_migrations(
        self, history_limit: int | None = None
    ) -> list[MigrationInfo]:
        rv: list[MigrationInfo] = []
        with self.Session.begin() as session:
            if history_limit:
                # Subquery to get the last N rows
                subq = (
                    sa.select(MigrationHistory)
                    .order_by(MigrationHistory.filename.desc())
                    .limit(history_limit)
                    .subquery()
                )

                # Re-select from subquery and sort ascending
                s = (
                    sa.select(MigrationHistory)
                    .join(subq, subq.c.filename == MigrationHistory.filename)
                    .order_by(MigrationHistory.filename)
                )
            else:
                # If no limit, just return all sorted ascending
                s = sa.select(MigrationHistory).order_by(MigrationHistory.filename)

            rows = session.execute(s).scalars().all()

        rv = [
            MigrationInfo(
                row.filename,
                parse_filename(row.filename)[0],
                row.executed_at.replace(tzinfo=UTC),
            )
            for row in rows
        ]
        rv += [
            MigrationInfo(file, parse_filename(file)[0], None)
            for file in self.list_migrations_to_upgrade()
        ]
        return rv
