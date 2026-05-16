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
from .models import CallJob, CallJobStatus, Employee, Job, JobStatus


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


@app.command(help="Re-run only the GHL match+note stage on an already-summarized job.")
def retry_match(id: str = typer.Option(..., "--id", help="Job id")) -> None:
    from .tasks.ghl import attach_note
    from .tasks.pipeline import _set_status  # noqa

    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.id == id).first()
        if not job:
            console.print("[red]not found[/red]")
            raise typer.Exit(code=1)
        if not job.summary:
            console.print(f"[red]job has no summary (status={job.status.value}) — re-upload required[/red]")
            raise typer.Exit(code=1)

        # Reset terminal state so attach_note can re-run cleanly.
        job.error_message = None
        job.completed_at = None
        job.ghl_contact_id = None
        job.ghl_note_id = None
        job.status = JobStatus.summarized
        db.commit()

        try:
            result = attach_note(db, job)
        except Exception as exc:
            console.print(f"[red]attach_note error:[/red] {exc}")
            raise typer.Exit(code=1)

        db.refresh(job)
        console.rule(f"retry-match result: {result.value}")
        console.print(f"contact_id: {job.ghl_contact_id or '—'}")
        console.print(f"note_id:    {job.ghl_note_id or '—'}")
        console.print(f"status:     {job.status.value}")
    finally:
        db.close()


@app.command(help="List recent GHL phone-call processing jobs.")
def list_call_jobs(
    job_status: str | None = typer.Option(None, "--status", help=f"Filter by status: {', '.join(s.value for s in CallJobStatus)}"),
    limit: int = typer.Option(30),
) -> None:
    db = SessionLocal()
    try:
        q = db.query(CallJob).order_by(CallJob.created_at.desc())
        if job_status:
            q = q.filter(CallJob.status == CallJobStatus(job_status))
        rows = q.limit(limit).all()
        table = Table(title=f"Call jobs ({len(rows)})")
        table.add_column("Created")
        table.add_column("Owner")
        table.add_column("Direction")
        table.add_column("Duration")
        table.add_column("Status")
        table.add_column("Contact")
        for r in rows:
            table.add_row(
                r.created_at.isoformat(timespec="seconds"),
                (r.ghl_user_name or "—")[:20],
                r.direction or "—",
                f"{r.duration_seconds // 60}:{r.duration_seconds % 60:02d}",
                r.status.value,
                r.ghl_contact_id[:10] if r.ghl_contact_id else "—",
            )
        console.print(table)
    finally:
        db.close()


@app.command(help="Trigger GHL call polling once, immediately (instead of waiting for the beat schedule).")
def poll_calls_now() -> None:
    from .tasks.phone_calls import poll_ghl_calls
    console.print("[cyan]polling GHL for new calls...[/cyan]")
    result = poll_ghl_calls()
    console.print(result)


@app.command(help="Re-run processing on a single CallJob (e.g. after a transient failure).")
def retry_call(id: str = typer.Option(..., "--id", help="CallJob id")) -> None:
    from .tasks.phone_calls import process_call_job
    db = SessionLocal()
    try:
        cj = db.query(CallJob).filter(CallJob.id == id).first()
        if not cj:
            console.print("[red]not found[/red]")
            raise typer.Exit(1)
        cj.error_message = None
        cj.completed_at = None
        cj.status = CallJobStatus.received
        db.commit()
    finally:
        db.close()
    console.print("[cyan]dispatching process_call_job...[/cyan]")
    result = process_call_job(id)
    console.print(result)


if __name__ == "__main__":
    app()
