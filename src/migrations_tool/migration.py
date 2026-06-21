import hashlib
import importlib.util
import logging
import textwrap
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple, Protocol, cast, runtime_checkable

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

log = logging.getLogger(__name__)
DATETIME_FMT = "%Y%m%d_%H%M_%S%f"

# Pinned primary key of the single-row schema_hash table.
SCHEMA_HASH_ROW_ID = 1
# Tip value for a database with no applied migrations.
GENESIS_HASH = hashlib.sha256(b"").hexdigest()


def normalize_content(raw: bytes) -> bytes:
    """Normalize line endings so the same file hashes identically regardless of
    the platform/git checkout that produced it (CRLF/CR -> LF)."""
    return raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def compute_content_hash(raw: bytes) -> str:
    """Hash of a single migration file's contents."""
    return hashlib.sha256(normalize_content(raw)).hexdigest()


def compute_chain_hash(prev_chain: str, filename: str, content_hash: str) -> str:
    """Fold one migration into the running chain (git-commit style)."""
    h = hashlib.sha256()
    h.update(prev_chain.encode())
    h.update(b"\0")
    h.update(filename.encode())
    h.update(b"\0")
    h.update(content_hash.encode())
    return h.hexdigest()


def compute_tip(rows: Iterable[tuple[str, str | None]]) -> str:
    """Cumulative chain hash over (filename, content_hash) pairs.

    `rows` MUST be supplied in sorted-filename order so the tip is independent of
    the order migrations were physically applied (i.e. holes filled later still
    yield the same value a fresh checkout would compute)."""
    tip = GENESIS_HASH
    for filename, content_hash in rows:
        tip = compute_chain_hash(tip, filename, content_hash or "")
    return tip


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


def _get_history_table_args() -> tuple[Any, ...]:
    try:
        from clickhouse_connect.cc_sqlalchemy.ddl.tableengine import ReplacingMergeTree

        return (ReplacingMergeTree(order_by="filename"),)
    except ImportError:
        return ()


def _get_schema_hash_table_args() -> tuple[Any, ...]:
    try:
        from clickhouse_connect.cc_sqlalchemy.ddl.tableengine import ReplacingMergeTree

        return (ReplacingMergeTree(order_by="id"),)
    except ImportError:
        return ()


@dataclass
class MigrationHistory(Base):
    __tablename__ = "migration_history"
    __table_args__ = _get_history_table_args()
    filename: Mapped[str] = mapped_column(sa.String(256), primary_key=True)
    executed_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
    )
    # sha256 of this migration's file contents at the time it was applied.
    # Nullable so databases created before this column existed can be backfilled.
    content_hash: Mapped[str | None] = mapped_column(
        sa.String(64),
        nullable=True,
        default=None,
    )


@dataclass
class SchemaHash(Base):
    """Single-row table holding the cumulative chain hash of all applied
    migrations. Cheap to read (`SELECT hash FROM schema_hash`) for consumers that
    only need to know whether the DB is up to date or matches a known value."""

    __tablename__ = "schema_hash"
    __table_args__ = _get_schema_hash_table_args()
    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
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
        # Bring the meta tables (content_hash column, schema_hash table) up to
        # date on any connection — this self-migrates databases initialized by
        # older versions of the tool that predate the hashing feature.
        self.ensure_meta_schema()

    @property
    def _is_clickhouse(self) -> bool:
        return self.engine.dialect.name.startswith("clickhouse")

    def ensure_meta_schema(self):
        """Idempotently migrate the tool's own bookkeeping tables and (re)seed
        the cumulative hash. Cheap when nothing is missing (metadata reads only)."""
        inspector = sa.inspect(self.engine)
        columns = {
            c["name"] for c in inspector.get_columns(MigrationHistory.__tablename__)
        }
        if "content_hash" not in columns:
            log.info("migrating meta schema: adding migration_history.content_hash")
            with self.engine.begin() as conn:
                conn.execute(
                    sa.text(
                        f"ALTER TABLE {MigrationHistory.__tablename__} "
                        f"ADD COLUMN content_hash VARCHAR(64)"
                    )
                )

        if not inspector.has_table(SchemaHash.__tablename__):
            log.info("migrating meta schema: creating schema_hash table")
            schema_hash_table = cast(sa.Table, SchemaHash.__table__)
            schema_hash_table.create(self.engine, checkfirst=True)

        self._backfill_and_seed_hash()

    def _backfill_and_seed_hash(self):
        """Fill in content_hash for rows recorded before the column existed (from
        current file contents) and (re)write the cumulative tip."""
        with self.Session.begin() as session:
            # In-place UPDATE is unsupported on ClickHouse (ReplacingMergeTree) and
            # re-inserting would leave transient duplicate rows that corrupt the
            # tip; ClickHouse support postdates this feature, so legacy rows there
            # simply keep a NULL content_hash.
            if not self._is_clickhouse:
                rows = (
                    session.execute(
                        sa.select(MigrationHistory).order_by(MigrationHistory.filename)
                    )
                    .scalars()
                    .all()
                )
                for row in rows:
                    if row.content_hash is not None:
                        continue
                    path = self.migrations_dir / row.filename
                    if path.exists():
                        row.content_hash = compute_content_hash(path.read_bytes())
                    else:
                        log.warning(
                            f"cannot backfill content_hash for applied migration "
                            f"`{row.filename}`: file is missing from "
                            f"{self.migrations_dir}"
                        )
            self._update_tip(session)

    def _update_tip(self, session: Session):
        """Recompute the cumulative chain hash from the applied rows (sorted) and
        store it in the single-row schema_hash table. Uses stored content_hash
        values, so already-applied migration files are never re-read here.

        The row is replaced with a raw DELETE + INSERT rather than an in-place
        ORM UPDATE: ClickHouse (ReplacingMergeTree) has no in-place UPDATE, and
        reading the current value via a plain query keeps it out of the ORM
        identity map so re-inserting the same primary key does not conflict."""
        rows = session.execute(
            sa.select(
                MigrationHistory.filename, MigrationHistory.content_hash
            ).order_by(MigrationHistory.filename)
        ).all()
        tip = compute_tip((r.filename, r.content_hash) for r in rows)

        table = SchemaHash.__tablename__
        current = session.execute(
            sa.text(f"SELECT hash FROM {table} WHERE id = :id"),  # noqa: S608
            {"id": SCHEMA_HASH_ROW_ID},
        ).scalar()
        if current == tip:
            return

        session.execute(
            sa.text(f"DELETE FROM {table} WHERE id = :id"),  # noqa: S608
            {"id": SCHEMA_HASH_ROW_ID},
        )
        session.flush()
        session.add(
            SchemaHash(id=SCHEMA_HASH_ROW_ID, hash=tip, updated_at=datetime.now(UTC))
        )
        session.flush()
        log.debug(f"schema hash tip updated to {tip}")

    def save_migration_history(self, filename: str, session: Session):
        log.debug(f"Recording migration execution: {filename})")
        content_hash = compute_content_hash(
            (self.migrations_dir / filename).read_bytes()
        )
        record = MigrationHistory(
            filename=filename,
            executed_at=datetime.now(UTC),
            content_hash=content_hash,
        )
        session.add(record)
        session.flush()
        self._update_tip(session)
        log.debug(f"Migration execution recorded: {filename}")
        return record

    def delete_migration_history(self, filename: str, session: Session):
        # Raw SQL DELETE so ClickHouse (which has no ORM-style DELETE) is supported.
        table = MigrationHistory.__tablename__
        session.execute(
            sa.text(f"DELETE FROM {table} WHERE filename = :filename"),  # noqa: S608
            {"filename": filename},
        )
        session.flush()
        self._update_tip(session)
        return filename

    def get_schema_hash(self) -> str:
        """The cumulative chain hash stored in the DB (cheap, single row)."""
        with self.Session.begin() as session:
            row = session.get(SchemaHash, SCHEMA_HASH_ROW_ID)
            return row.hash if row is not None else GENESIS_HASH

    def compute_expected_hash(self) -> str:
        """Chain hash computed over the migration files currently on disk (sorted).
        Equals get_schema_hash() iff the DB has applied exactly those files with
        matching contents."""
        return compute_tip(
            (
                filename,
                compute_content_hash((self.migrations_dir / filename).read_bytes()),
            )
            for filename in self.list_existing_migration_files()
        )

    def is_up_to_date(self) -> bool:
        return self.get_schema_hash() == self.compute_expected_hash()

    def find_drift(self) -> list[str]:
        """Applied migrations whose on-disk content no longer matches what was
        recorded when they were applied (or whose file is now missing)."""
        with self.Session.begin() as session:
            rows = session.execute(
                sa.select(MigrationHistory.filename, MigrationHistory.content_hash)
            ).all()
        drifted: list[str] = []
        for filename, stored in sorted(rows):
            path = self.migrations_dir / filename
            if not path.exists():
                drifted.append(filename)
            elif (
                stored is not None and compute_content_hash(path.read_bytes()) != stored
            ):
                drifted.append(filename)
        return drifted

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
        db_update = (
            self.save_migration_history if upgrade else self.delete_migration_history
        )
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

            def get_inspector(session:Session) -> sa.Inspector:
                return sa.inspect(session.connection())

            def upgrade(session: Session):
                pass

            def downgrade(session: Session):
                pass
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
