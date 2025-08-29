import logging
import signal
import sys
from collections.abc import Iterable
from sqlalchemy.exc import ArgumentError, OperationalError
import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.style import Style
from rich.table import Table
from rich.text import Text
from typing import TypedDict
from .migration import MigrationInfo, MigrationTool, MigrationsToolError

log = logging.getLogger(__name__)
app = typer.Typer(help="Custom SQLAlchemy Migration Tool")
console = Console()
database_url_opt = typer.Option(
    ...,
    "--database-url",
    help="Database connection URL",
    envvar="WRITER_DATABASE_URI",
)
migrations_dir_opt = typer.Option(
    "migrations",
    "--migrations-dir",
    help="Migrations directory",
    envvar="MIGRATIONS_DIR",
)
database_echo_opt = typer.Option(
    default=False,
    envvar="DATABASE_ECHO",
    help="print all sql stmt executed to stdout",
)


class Config(TypedDict):
    database_url: str
    migrations_dir: str
    database_echo: bool


_config: Config | None = None


def get_tool(init: bool = False):
    assert _config is not None

    return MigrationTool(
        _config["database_url"],
        _config["migrations_dir"],
        _config["database_echo"],
        should_create_history_table=init,
    )


@app.command()
def generate(description: str = typer.Argument(..., help="Migration description")):
    """Generate new migration"""
    tool = get_tool()

    filepath = tool.generate_migration(description)
    file_link = f"[link={filepath.absolute().as_uri()}]{filepath}[/link]"
    console.print(
        Panel(
            f"New empty migration file at : {file_link}",
            title="Created",
            style=Style(color="green"),
            expand=False,
        )
    )


@app.command()
def upgrade(
    target_file: str | None = typer.Option(None, help="Target migration filename"),
):
    """Run pending migrations"""

    tool = get_tool()
    files = tool.list_migrations_to_upgrade(target_filename=target_file)
    if not files:
        console.print(
            Panel(
                "no migrations to apply",
                title="Completed",
                expand=False,
                style=Style(color="blue"),
            )
        )
        return

    table = Table("filename", title="Migrations To Apply")
    for file in files:
        table.add_row(file)

    console.print(table)
    _ = process_migrations(
        tool.lazy_execute(files, upgrade=True),
        "Running Upgrade Migrations",
        len(files),
    )
    show_table(tool.list_all_migrations(history_limit=10))


@app.command(
    help="downgrade to a specific migration file, will show the migration that will be downgraded with manual confirmation"
)
def downgrade(
    filename: str | None = typer.Option(
        None,
        help="filename to downgrade to starting at current",
    ),
    version_to_downgrade_count: int = typer.Option(1, "-n"),
    auto_confirm: bool = typer.Option(False, "-y"),
):
    """Downgrade to revision"""

    tool = get_tool()
    if filename:
        target_filename = filename
    else:
        version_to_downgrade_count += 1
        target_filename = tool.list_applied_migration_files()[
            -version_to_downgrade_count
        ]
    files = tool.list_migrations_to_downgrade(target_filename=target_filename)
    if not files:
        console.print("no files to downgrade were found")
        return
    table = Table("filename", title="Migration to downgrade")
    for file in files:
        table.add_row(file)

    console.print(table)
    if not auto_confirm:
        if not typer.confirm(
            "the migrations listed in the table will be downgrade , apply?"
        ):
            exit(1)

    process_migrations(
        tool.lazy_execute(files, upgrade=False),
        f"Downgrade -> {filename}",
        len(files),
    )
    show_list()


@app.command("list", help="show a list of all migrations")
def show_list(
    limit: int = typer.Option(default=20, help="limit row output"),
):
    """Show migration status"""
    tool = get_tool()
    show_table(tool.list_all_migrations(history_limit=limit))


@app.command(help="output to stdout the current migration revision_id")
def current():
    tool = get_tool()
    typer.echo(tool.get_current_revision(), color=False)


@app.command(help="call this once to create the history table in the database")
def init():
    tool = get_tool(init=True)
    tool.generate_migration("first_migration_empty")
    _ = list(tool.lazy_execute(tool.list_migrations_to_upgrade(), upgrade=True))
    console.print(
        Panel(
            Text("init called created table `migration_history`"),
            title="Completed",
            style="green",
            expand=False,
        )
    )


@app.command(help="output to stdout pending revision_id 1 per line")
def pending():
    tool = get_tool()
    files = tool.list_migrations_to_upgrade()
    typer.echo("\n".join(files))


def show_table(items: Iterable[MigrationInfo]):
    table = Table(
        title="Migration",
        show_header=True,
    )
    table.add_column("status")
    table.add_column("filename")
    table.add_column("created_at")
    table.add_column("executed_at")
    datefmt = "%Y-%m%d %H:%M:%S %Z"
    for item in items:
        status = (
            Text("applied", style=Style(color="green", bold=True))
            if item.executed_at
            else Text("pending", style=Style(color="yellow", bold=True))
        )
        table.add_row(
            status,
            item.filename,
            item.created_at.strftime(datefmt),
            item.executed_at.strftime(datefmt) if item.executed_at else "",
        )
    if table.row_count:
        console.print(table)


def process_migrations(files: Iterable[str], description: str, size: int) -> list[str]:
    """Consume iterator with progress bar, return list of processed items"""
    results: list[str] = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TextColumn("•"),
        TextColumn("[cyan]{task.completed}/{task.total}[/cyan] migrations"),
        TextColumn("•"),
        TimeElapsedColumn(),
        TextColumn("•"),
        TimeRemainingColumn(),
        TextColumn("•"),
        TextColumn("[yellow]{task.fields[current_file]}[/yellow]"),
    ) as progress:
        task = progress.add_task(description, total=size, current_file="Starting...")

        for idx, file in enumerate(files, 1):
            # Update current file being processed
            progress.update(
                task,
                current_file=f"Processing: {file}",
                description=f"{description} [{idx}/{size}]",
            )

            # Process here
            # ... your migration logic ...

            results.append(file)  # or whatever the result is

            progress.update(task, advance=1)

        # Final update
        progress.update(task, current_file="✓ Complete!")

    return results


@app.callback()
def main_callback(
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Enable verbose logging",
    ),
    log_format: str = typer.Option(
        default="%(levelname).1s %(asctime)s.%(msecs)03d - %(processName)s:%(threadName)s - f:%(funcName)s l:%(lineno)s - %(message)s",
        help="must comply with python stdlib logging module",
    ),
    log_level: str = typer.Option(
        default="INFO",
        help="<DEBUG | INFO | WARNING | ERROR>",
    ),
    database_url: str = database_url_opt,
    migrations_dir: str = migrations_dir_opt,
    echo: bool = database_echo_opt,
):
    """Configure logging before running any command"""
    # Override with verbose flag if set
    if verbose:
        log_level = "DEBUG"

    global _config
    _config = {
        "database_echo": echo,
        "database_url": database_url,
        "migrations_dir": migrations_dir,
    }

    # Configure logging
    logging.basicConfig(
        stream=sys.stdout,
        level=logging.getLevelNamesMapping()[log_level],
        force=True,
        format=log_format,
        datefmt="%Y/%m/%d %H:%M:%S",
    )


def main():
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    try:
        app()
    except (
        MigrationsToolError,
        ArgumentError,
        OperationalError,
    ) as e:
        console.print(
            Panel(
                Text(str(e)),
                title="ERROR",
                style=Style(color="red", bold=True),
                expand=False,
            )
        )
        exit(1)
    except Exception as e:
        log.exception("unhandled error")
        console.print(
            Panel(
                Text(str(e)),
                title="ERROR",
                style=Style(color="red", bold=True),
                expand=False,
            )
        )
        exit(1)


if __name__ == "__main__":
    main()
