"""CLI entry point: zghl <command>"""

import logging
import signal
import sys
import time
from pathlib import Path

import httpx
import typer
from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.table import Table

from . import APP_NAME, __version__, db
from .config import Config
from .paths import app_data_dir, config_path, default_zoom_folder, local_db_path
from .uploader import handle_recording
from .watcher import scan


app = typer.Typer(no_args_is_help=True, add_completion=False, help=f"{APP_NAME} uploader v{__version__}")
console = Console()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("zghl")


@app.command(help="הגדרה אינטראקטיבית של החיבור לשרת והנתיבים.")
def init() -> None:
    cfg = Config.load()
    console.rule(f"{APP_NAME} — הגדרה ראשונית")

    cfg.employee_name = Prompt.ask("שם העובדת (יוצג ב-GHL ובלוגים)", default=cfg.employee_name or "")
    cfg.api_key = Prompt.ask("API key שקיבלת מגד", default=cfg.api_key or "", password=True)
    cfg.server_url = Prompt.ask("כתובת השרת", default=cfg.server_url)
    cfg.watch_folder = Prompt.ask("תיקיית הקלטות זום", default=cfg.watch_folder or str(default_zoom_folder()))
    cfg.move_after_upload = Confirm.ask("להעביר קבצים ל-uploaded/ אחרי העלאה?", default=cfg.move_after_upload)

    cfg.save()
    db.init_db()
    console.print(f"[green]✓ נשמר ב-{config_path()}[/green]")
    console.print(f"[dim]DB מקומי: {local_db_path()}[/dim]")
    console.print("\nלהרצת בדיקה: [bold]zghl test-connection[/bold]")
    console.print("לסריקה חד-פעמית: [bold]zghl scan[/bold]")
    console.print("להפעלה רציפה: [bold]zghl run[/bold]")


@app.command(help="בדיקת חיבור: ping ל-/health ול-/jobs.")
def test_connection() -> None:
    cfg = Config.load()
    if not cfg.is_configured():
        console.print("[red]חסר config — הרץ 'zghl init' קודם[/red]")
        raise typer.Exit(1)

    with httpx.Client(timeout=10.0) as c:
        try:
            r = c.get(f"{cfg.server_url.rstrip('/')}/health")
            console.print(f"GET /health → {r.status_code} {r.text[:100]}")
        except httpx.HTTPError as e:
            console.print(f"[red]/health failed: {e}[/red]")
            raise typer.Exit(1)

        try:
            r = c.get(f"{cfg.server_url.rstrip('/')}/jobs", headers={"X-API-Key": cfg.api_key})
            if r.status_code == 200:
                jobs = r.json()
                console.print(f"[green]✓ Auth OK[/green] — {len(jobs)} jobs קודמים")
            else:
                console.print(f"[red]/jobs failed: {r.status_code} {r.text[:200]}[/red]")
                raise typer.Exit(1)
        except httpx.HTTPError as e:
            console.print(f"[red]/jobs failed: {e}[/red]")
            raise typer.Exit(1)


@app.command(help="סריקה חד-פעמית של תיקיית הזום. לבדיקה ופיתוח.")
def scan_once(dry_run: bool = typer.Option(False, "--dry-run", help="הצג מה תועלה — בלי להעלות")) -> None:
    cfg = Config.load()
    if not cfg.is_configured():
        console.print("[red]חסר config — הרץ 'zghl init' קודם[/red]")
        raise typer.Exit(1)
    db.init_db()
    _scan_pass(cfg, dry_run=dry_run)


@app.command(help="הפעלה רציפה — סורק כל N דקות עד עצירה.")
def run() -> None:
    cfg = Config.load()
    if not cfg.is_configured():
        console.print("[red]חסר config — הרץ 'zghl init' קודם[/red]")
        raise typer.Exit(1)
    db.init_db()

    stop = {"flag": False}
    def _stop(*_):
        stop["flag"] = True
        console.print("[yellow]מקבל אות עצירה, מסיים סבב נוכחי...[/yellow]")
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    console.print(f"[green]ZoomGHL פועל. סורק כל {cfg.scan_interval_minutes} דקות. Ctrl+C לעצירה.[/green]")
    while not stop["flag"]:
        try:
            _scan_pass(cfg, dry_run=False)
        except Exception as exc:
            log.exception("scan pass crashed", extra={"err": str(exc)})
        if stop["flag"]:
            break
        # Sleep in 5s ticks so SIGINT lands quickly.
        for _ in range(cfg.scan_interval_minutes * 60 // 5):
            if stop["flag"]:
                break
            time.sleep(5)
    console.print("[dim]נעצר.[/dim]")


@app.command(help="הצגת הסטטוס הנוכחי וסטטיסטיקות.")
def status() -> None:
    cfg = Config.load()
    console.rule(f"{APP_NAME} status")
    console.print(f"שם עובדת:        {cfg.employee_name or '—'}")
    console.print(f"שרת:             {cfg.server_url}")
    console.print(f"תיקיית הקלטות:   {cfg.watch_folder}")
    console.print(f"מעבר ל-uploaded/: {'כן' if cfg.move_after_upload else 'לא'}")
    console.print(f"config path:     {config_path()}")
    console.print(f"local DB:        {local_db_path()}")
    console.print()
    db.init_db()
    s = db.stats()
    table = Table(title="העלאות מקומיות")
    table.add_column("status"); table.add_column("count")
    for k in ("uploaded", "failed"):
        table.add_row(k, str(s.get(k, 0)))
    console.print(table)
    if s.get("last_upload"):
        console.print(f"[dim]ההעלאה האחרונה: {s['last_upload']['folder_name']} ({s['last_upload']['uploaded_at']})[/dim]")


def _scan_pass(cfg: Config, dry_run: bool) -> None:
    watch = Path(cfg.watch_folder)
    log.info(f"scanning {watch}")
    recordings = scan(watch)
    if not recordings:
        log.info("no recordings ready")
        return

    with httpx.Client(timeout=httpx.Timeout(connect=10.0, read=600.0, write=600.0, pool=10.0)) as client:
        for rec in recordings:
            if db.was_uploaded(rec.file):
                log.info(f"already uploaded — skip: {rec.file.name} in {rec.folder_name}")
                continue
            if dry_run:
                log.info(f"[dry-run] would upload: {rec.file.name} ({rec.folder_name})")
                continue
            result = handle_recording(cfg, rec, client=client)
            if result.ok:
                log.info(f"✓ uploaded {rec.folder_name} → job {result.job_id}")
            else:
                log.warning(f"✗ failed {rec.folder_name}: {result.error}")


if __name__ == "__main__":
    app()
