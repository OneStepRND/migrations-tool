# Migrations Tool

A lightweight database migration tool for managing schema changes and data migrations using SQLAlchemy, with built-in support for MySQL-specific utilities.

## Overview

This tool provides a simple, file-based migration system that tracks applied migrations in a `migration_history` table. Each migration is a Python file containing `upgrade()` and `downgrade()` functions.

## Features

- File-based migrations with timestamp ordering
- Automatic tracking of applied migrations
- Support for upgrade and downgrade operations
- Target-specific migrations (migrate to a specific version)
- Migration generation with timestamps
- Session-based execution with SQLAlchemy ORM support
- Rich CLI interface with progress bars and colored output
- MySQL utilities for schema introspection
- Environment variables or cli args for configuration

## Installation

### Install from GitHub release
```bash
pip install git+https://github.com/username/migrations-tool.git@v0.1.0
```

### For local development:
```bash
uv sync --frozen --dev
```

### docker build
```bash
docker build -t migrations-tool .
```

## CLI Usage

The tool provides a command-line interface accessible via the `db-migrate` command:

### Initialize the migration system
```bash
db-migrate init
```

### Generate a new migration
```bash
db-migrate generate "add user table"
```

### Run pending migrations
```bash
db-migrate upgrade
```

### Run migrations to a specific target
```bash
db-migrate upgrade --target-file "20250829_1624_57655025__first_migration_empty.py"
```

### Downgrade migrations
```bash
db-migrate downgrade -n 1  # Downgrade 1 migration
db-migrate downgrade --filename "target_migration.py"  # Downgrade to specific migration
```

### List migration status
```bash
db-migrate list --limit 20
```

### Show current migration
```bash
db-migrate current
```

### Show pending migrations
```bash
db-migrate pending
```

## Configuration

The tool can be configured via environment variables or command-line options:

- `MIGRATIONS_TOOL_DATABASE_URL`: Database connection URL (required)
- `MIGRATIONS_TOOL_DIR`: Migrations directory (default: "migrations")
- `MIGRATIONS_TOOL_DATABASE_ECHO`: Echo SQL statements (default: false)
- `MIGRATIONS_TOOL_VERBOSE`: Enable debug logging (default: false)
- `MIGRATIONS_TOOL_LOG_FORMAT`: Custom log format
- `MIGRATIONS_TOOL_LOG_LEVEL`: Log level (DEBUG|INFO|WARNING|ERROR)

Example:
```bash
export MIGRATIONS_TOOL_DATABASE_URL="mysql://user:password@localhost:3306/database_name"
export MIGRATIONS_TOOL_VERBOSE=true
db-migrate list
```

### Programmatic Usage

```python
from migrations_tool.migration import MigrationTool

# Initialize the tool
migrator = MigrationTool(
    database_url="mysql://user:password@localhost:3306/database_name",
    migrations_dir="migrations",
    database_echo=False,
    should_create_history_table=True  # Only on first run
)

# Generate a new migration file
filepath = migrator.generate_migration("add_user_table")
# Creates: migrations/YYYYMMDD_HHMMSS_microseconds__add_user_table.py
```

The generated file will contain:
```python
import sqlalchemy as sa
from sqlalchemy.orm import Session

def upgrade(session: Session):
    pass

def downgrade(session: Session):
    pass
```

### Writing Migrations

Edit the generated file to add your migration logic:

```python
import sqlalchemy as sa
from sqlalchemy.orm import Session
from migrations_tool.mysql import check_table_exists, check_column_exists

def upgrade(session: Session):
    session.execute(sa.text("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTO_INCREMENT,
            username VARCHAR(50) NOT NULL,
            email VARCHAR(100) NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """))

def downgrade(session: Session):
    session.execute(sa.text("DROP TABLE users"))
```

### MySQL Utilities

The tool includes MySQL-specific utilities for safer migrations:

```python
from migrations_tool.mysql import (
    check_table_exists,
    check_column_exists,
    check_index_exists,
    check_constraint_exists
)

def upgrade(session: Session):
    if not check_table_exists(session, "users"):
        session.execute(sa.text("""
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTO_INCREMENT,
                username VARCHAR(50) NOT NULL
            )
        """))

    if not check_column_exists(session, "users", "email"):
        session.execute(sa.text("ALTER TABLE users ADD COLUMN email VARCHAR(100)"))
```

## Migration File Naming Convention

Migration files follow a strict naming pattern:
```
YYYYMMDD_HHMMSS_microseconds__description.py
```

Example: `20240115_143052_123456__add_user_table.py`

The timestamp ensures proper ordering, and the description helps identify the migration's purpose.

## Important Considerations

### MySQL and Transactional DDL

**⚠️ WARNING: MySQL does not support transactional DDL (Data Definition Language) statements.**

Unlike PostgreSQL or SQLite, MySQL will automatically commit DDL statements (CREATE TABLE, ALTER TABLE, DROP TABLE, etc.) immediately, regardless of transaction boundaries. This means:

1. **DDL changes cannot be rolled back** - If a migration fails after executing a DDL statement, the schema change will persist even though the migration is marked as failed.


2. **Mixed DDL/DML migrations are risky** - Combining schema changes with data modifications in a single migration can lead to partial application if the migration fails.

3. **Best practices for MySQL:**
   - Keep DDL and DML operations in separate migration files
   - Make DDL operations idempotent where possible
   - Test migrations thoroughly in a non-production environment
   - Consider using `IF EXISTS` / `IF NOT EXISTS` clauses
   - Have a rollback strategy that doesn't rely on transactions

Example of a safer MySQL migration:
```python
def upgrade(session: Session):
    # DDL operations will auto-commit in MySQL
    session.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTO_INCREMENT,
            username VARCHAR(50) NOT NULL
        )
    """))

def downgrade(session: Session):
    # This will also auto-commit immediately
    session.execute(sa.text("DROP TABLE IF EXISTS users"))
```

### Other Database Considerations

- **PostgreSQL**: Fully supports transactional DDL - migrations are atomic
- **SQLite**: Supports transactional DDL with some limitations

## Error Handling

The tool will raise exceptions for:
- Missing migration files
- Invalid migration file format
- Missing `upgrade()` or `downgrade()` functions
- Database connection issues

## Best Practices

1. **Always test migrations** in a development environment first
2. **Keep migrations small and focused** - one logical change per migration
3. **Make migrations reversible when possible** - implement both upgrade and downgrade
4. **Use descriptive names** for migrations
5. **Avoid modifying old migrations** - create new ones to fix issues
6. **Back up your database** before running migrations in production
7. **Version control your migrations** alongside your application code
8. **Document complex migrations** with comments explaining the reasoning
9. **Consider data migrations carefully** - they can be slow on large tables
10. **Be extra cautious with MySQL** due to its lack of transactional DDL