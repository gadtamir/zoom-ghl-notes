"""Admin CLI: manage employees and inspect jobs.

Usage:
    python -m app.cli add-employee --name "Sarah"
    python -m app.cli list-employees
    python -m app.cli deactivate --id <employee-id>
    python -m app.cli rotate-key --id <employee-id>
    python -m app.cli list-jobs [--employee NAME] [--status STATUS]
"""

import typer
from rich.console import Console
from rich.table import Table

from .auth import generate_api_key
from .db import Base, SessionLocal, engine
from .models import Employee, Job, JobStatus


app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()


@app.command(help="Create database tables (idempotent).")
def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    console.print("[green]OK[/green] tables created.")


@app.command(help="Create a new employee and print their API key (shown only once).")
def add_employee(name: str = typer.Option(..., help="Employee display name")) -> None:
    db = SessionLocal()
    try:
        full_key, prefix, key_hash = generate_api_key()
        emp = Employee(name=name, api_key_prefix=prefix, api_key_hash=key_hash, active=True)
        db.add(emp)
        db.commit()
        db.refresh(emp)
        console.print(f"[green]Created employee[/green] [bold]{emp.name}[/bold] (id={emp.id})")
        console.rule("API KEY — copy now, will not be shown again")
        console.print(f"[bold yellow]{full_key}[/bold yellow]")
        console.rule()
    finally:
        db.close()


@app.command(help="List all employees.")
def list_employees() -> None:
    db = SessionLocal()
    try:
        rows = db.query(Employee).order_by(Employee.created_at).all()
        table = Table(title="Employees")
        table.add_column("ID")
        table.add_column("Name")
        table.add_column("Key prefix")
        table.add_column("Active")
        table.add_column("Created")
        for e in rows:
            table.add_row(e.id, e.name, e.api_key_prefix, "yes" if e.active else "no", e.created_at.isoformat(timespec="seconds"))
        console.print(table)
    finally:
        db.close()


@app.command(help="Deactivate an employee (their key stops working).")
def deactivate(id: str = typer.Option(..., help="Employee id")) -> None:
    db = SessionLocal()
    try:
        emp = db.query(Employee).filter(Employee.id == id).first()
        if not emp:
            console.print("[red]not found[/red]")
            raise typer.Exit(code=1)
        emp.active = False
        db.commit()
        console.print(f"[yellow]Deactivated[/yellow] {emp.name}")
    finally:
        db.close()


@app.command(help="Generate a new API key for an existing employee.")
def rotate_key(id: str = typer.Option(..., help="Employee id")) -> None:
    db = SessionLocal()
    try:
        emp = db.query(Employee).filter(Employee.id == id).first()
        if not emp:
            console.print("[red]not found[/red]")
            raise typer.Exit(code=1)
        full_key, prefix, key_hash = generate_api_key()
        emp.api_key_prefix = prefix
        emp.api_key_hash = key_hash
        db.commit()
        console.rule(f"New API key for {emp.name}")
        console.print(f"[bold yellow]{full_key}[/bold yellow]")
        console.rule()
    finally:
        db.close()


@app.command(help="List recent jobs.")
def list_jobs(
    employee: str | None = typer.Option(None, help="Filter by employee name (substring)"),
    job_status: str | None = typer.Option(None, "--status", help=f"Filter by status: {', '.join(s.value for s in JobStatus)}"),
    limit: int = typer.Option(30),
) -> None:
    db = SessionLocal()
    try:
        q = db.query(Job).order_by(Job.created_at.desc())
        if employee:
            q = q.filter(Job.employee_name.ilike(f"%{employee}%"))
        if job_status:
            q = q.filter(Job.status == JobStatus(job_status))
        rows = q.limit(limit).all()

        table = Table(title=f"Jobs (latest {len(rows)})")
        table.add_column("Created")
        table.add_column("Employee")
        table.add_column("File")
        table.add_column("Status")
        table.add_column("Contact")
        for j in rows:
            table.add_row(
                j.created_at.isoformat(timespec="seconds"),
                j.employee_name,
                j.original_filename[:40],
                j.status.value,
                j.extracted_contact_name or "—",
            )
        console.print(table)
    finally:
        db.close()


if __name__ == "__main__":
    app()
