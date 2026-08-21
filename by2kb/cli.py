from __future__ import annotations

import asyncio
import json

import typer

from by2kb import __version__
from by2kb.config import load_config
from by2kb.jobs.runner import ingest_url
from by2kb.jobs.store import JobStore

app = typer.Typer(help="by2kb — forward a video, keep the knowledge")


def _configure_stdio() -> None:
    import sys

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass


@app.command()
def ingest(
    url: str = typer.Argument(..., help="Video URL (Bilibili for now)"),
    refresh: bool = typer.Option(False, "--refresh", help="Bypass idempotent reuse"),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable result"),
) -> None:
    _configure_stdio()
    config = load_config()
    outcome = asyncio.run(ingest_url(url, config, refresh=refresh))
    if json_out:
        typer.echo(json.dumps(outcome.to_dict(), ensure_ascii=False))
    else:
        typer.echo(outcome.message)
    raise typer.Exit(outcome.exit_code)


@app.command()
def status(
    job_id: str = typer.Argument(...),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    _configure_stdio()
    config = load_config()
    store = JobStore(config.db_path)
    try:
        job = store.get_job(job_id)
        if job is None:
            typer.echo(f"job not found: {job_id}", err=True)
            raise typer.Exit(1)
        payload = {
            "job_id": job.id,
            "platform": job.platform,
            "video_id": job.video_id,
            "status": job.status.value,
            "last_error_category": job.last_error_category,
            "error_message": job.error_message,
            "artifacts": store.artifacts(job.id),
        }
        if json_out:
            typer.echo(json.dumps(payload, ensure_ascii=False))
        else:
            typer.echo(f"{job.platform}/{job.video_id} — {job.status.value}")
            for artifact in payload["artifacts"]:
                typer.echo(f"  {artifact['kind']}: {artifact['path']}")
    finally:
        store.close()


@app.command()
def version() -> None:
    typer.echo(__version__)


if __name__ == "__main__":
    app()
