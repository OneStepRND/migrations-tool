# Migrations Tool

A lightweight database migration tool for managing schema changes and data migrations using SQLAlchemy.

## Overview

This tool provides a simple, file-based migration system that tracks applied migrations in a `migration_history` table. Each migration is a Python file containing `upgrade()` and `downgrade()` functions.

## Features

- File-based migrations with timestamp ordering
- Automatic tracking of applied migrations
- Support for upgrade and downgrade operations
- Target-specific migrations (migrate to a specific version)
- Migration generation with timestamps
- Session-based execution with SQLAlchemy ORM support

## Installation

For local development:
```bash
uv sync --frozen --extra=dev
```

For normal use:
```bash
uv sync --frozen
```

## Usage

### Basic Setup

```python
from migration_tool import MigrationTool

# Initialize the tool
migrator = MigrationTool(
    database_url="mysql://user:password@localhost:3306/database_name",
    migrations_dir="migrations",
    database_echo=False,
    should_create_history_table=True  # Only on first run
)
```

### Creating a New Migration

```python
# Generate a new migration file
filepath = migrator.generate_migration("add_user_table")
# Creates: migrations/20240115_143052_123456__add_user_table.py
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

def upgrade(session: Session):
    session.execute(sa.text("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username VARCHAR(50) NOT NULL,
            email VARCHAR(100) NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """))

def downgrade(session: Session):
    session.execute(sa.text("DROP TABLE users"))
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