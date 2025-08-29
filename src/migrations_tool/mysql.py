from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session


def _execute_exists_query(
    session: Session,
    stmt: sa.TextClause,
    params: dict[str, Any],
) -> bool:
    return bool(session.execute(stmt, params).scalar())


def check_table_exists(session: Session, table: str) -> bool:
    stmt = sa.text("""
       SELECT COUNT(*)
       FROM information_schema.tables
       WHERE table_schema = DATABASE()
       AND table_name = :table_name
   """)
    return _execute_exists_query(session, stmt, {"table_name": table})


def check_column_exists(session: Session, table: str, column: str) -> bool:
    stmt = sa.text("""
       SELECT COUNT(*)
       FROM information_schema.columns
       WHERE table_schema = DATABASE()
       AND table_name = :table_name
       AND column_name = :column_name
   """)
    return _execute_exists_query(
        session, stmt, {"table_name": table, "column_name": column}
    )


def check_index_exists(session: Session, table: str, index: str) -> bool:
    stmt = sa.text("""
       SELECT COUNT(*)
       FROM information_schema.statistics
       WHERE table_schema = DATABASE()
       AND table_name = :table_name
       AND index_name = :index_name
   """)
    return _execute_exists_query(
        session, stmt, {"table_name": table, "index_name": index}
    )


def check_constraint_exists(session: Session, table: str, constraint: str) -> bool:
    stmt = sa.text("""
       SELECT COUNT(*)
       FROM information_schema.table_constraints
       WHERE constraint_schema = DATABASE()
       AND table_name = :table_name
       AND constraint_name = :constraint_name
   """)
    return _execute_exists_query(
        session, stmt, {"table_name": table, "constraint_name": constraint}
    )
