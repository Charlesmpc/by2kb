from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer

from by2kb import __version__
from by2kb.agent_install import install_hermes_plugin
from by2kb.config import default_home, load_config
from by2kb.errors import By2kbError, ConfigError
from by2kb.jobs.enrichment_service import (
    claim_external_enrichment,
    complete_external_enrichment,
    fail_external_enrichment,
)
from by2kb.jobs.runner import ingest_source
from by2kb.jobs.store import JobStore
from by2kb.providers.asr_faster_whisper import (
    FasterWhisperConfig,
    faster_whisper_status,
    install_faster_whisper_model,
)
from by2kb.setup import InitSettings, write_initial_config

app = typer.Typer(help="by2kb — forward a video, keep the knowledge")
enrichment_app = typer.Typer(help="External-agent enrichment protocol")
agent_app = typer.Typer(help="Install by2kb into an agent host")
models_app = typer.Typer(help="Inspect and install optional local ASR models")
app.add_typer(enrichment_app, name="enrichment")
app.add_typer(agent_app, name="agent")
app.add_typer(models_app, name="models")


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
    source: str = typer.Argument(..., help="Bilibili URL or local audio/video path"),
    refresh: bool = typer.Option(False, "--refresh", help="Bypass idempotent reuse"),
    re_enrich: bool = typer.Option(
        False,
        "--re-enrich",
        help="Regenerate summaries from stored transcript without refetching media",
    ),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable result"),
    enricher: str | None = typer.Option(
        None,
        "--enricher",
        help="Summary executor: auto, api, external_agent, or disabled",
    ),
) -> None:
    _configure_stdio()
    config = load_config()
    if refresh and re_enrich:
        raise typer.BadParameter("--refresh and --re-enrich cannot be used together")
    outcome = asyncio.run(
        ingest_source(
            source,
            config,
            refresh=refresh,
            re_enrich=re_enrich,
            enricher=enricher,
        )
    )
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


@models_app.command("status")
def models_status(
    model: str | None = typer.Argument(None, help="Whisper model override"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Inspect the selected faster-whisper dependency and model cache."""
    _configure_stdio()
    config = load_config()
    options = dict(config.asr_options)
    if model:
        options["model"] = model
    whisper = FasterWhisperConfig.from_mapping(options, home=config.home)
    payload = faster_whisper_status(whisper)
    if json_out:
        typer.echo(json.dumps(payload, ensure_ascii=False))
        return
    dependency = "installed" if payload["dependency_installed"] else "missing"
    model_state = "installed" if payload["model_installed"] else "missing"
    typer.echo(f"faster-whisper dependency: {dependency}")
    typer.echo(f"model {payload['model']}: {model_state}")
    typer.echo(f"path: {payload['model_path']}")


@models_app.command("install")
def models_install(
    model: str | None = typer.Argument(None, help="Whisper model override"),
) -> None:
    """Explicitly download a faster-whisper model into the by2kb model cache."""
    _configure_stdio()
    config = load_config()
    options = dict(config.asr_options)
    if model:
        options["model"] = model
    whisper = FasterWhisperConfig.from_mapping(options, home=config.home)
    current = faster_whisper_status(whisper)
    if current["model_installed"]:
        typer.echo(f"Model already installed: {current['model_path']}")
        return
    typer.echo(f"Downloading faster-whisper model {whisper.model}...")
    try:
        path = install_faster_whisper_model(whisper)
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(exc.exit_code) from exc
    typer.echo(f"Installed model: {path}")


@app.command("init")
def init_config(
    home: Path = typer.Option(default_home(), "--home", help="Configuration folder"),
    force: bool = typer.Option(False, "--force", help="Replace existing configuration"),
) -> None:
    """Guide a user through ASR, temporary TOS, summaries, and output storage."""
    _configure_stdio()
    typer.echo("Configure by2kb (secrets are stored locally in .env).")
    library_root = Path(
        typer.prompt("Knowledge-base folder", default=str(home / "library"))
    ).expanduser()
    tos_access_key = typer.prompt("TOS access key", hide_input=True)
    tos_secret_key = typer.prompt("TOS secret key", hide_input=True)
    tos_bucket = typer.prompt("Private TOS bucket")
    tos_region = typer.prompt("TOS region", default="ap-southeast-1")
    tos_endpoint = typer.prompt(
        "TOS S3 endpoint (blank uses the regional default)", default=""
    )
    auth_mode = typer.prompt(
        "Doubao ASR authentication (api-key or legacy)", default="api-key"
    )
    if auth_mode not in {"api-key", "legacy"}:
        raise typer.BadParameter("ASR authentication must be api-key or legacy")
    doubao_api_key = ""
    doubao_app_id = ""
    doubao_access_token = ""
    if auth_mode == "api-key":
        doubao_api_key = typer.prompt("Doubao ASR API key", hide_input=True)
    else:
        doubao_app_id = typer.prompt("Doubao app id")
        doubao_access_token = typer.prompt("Doubao access token", hide_input=True)

    enrichment_mode = typer.prompt(
        "Summary mode (agent, api, or disabled)", default="agent"
    )
    executor = {"agent": "external_agent", "api": "api", "disabled": "disabled"}.get(
        enrichment_mode
    )
    if executor is None:
        raise typer.BadParameter("summary mode must be agent, api, or disabled")
    llm_api_key = ""
    llm_model = ""
    llm_base_url = "https://ark.cn-beijing.volces.com/api/v3"
    if executor == "api":
        llm_base_url = typer.prompt("LLM API base URL", default=llm_base_url)
        llm_model = typer.prompt("LLM model")
        llm_api_key = typer.prompt("LLM API key", hide_input=True)

    settings = InitSettings(
        library_root=library_root,
        enrichment_executor=executor,
        tos_access_key=tos_access_key,
        tos_secret_key=tos_secret_key,
        tos_bucket=tos_bucket,
        tos_region=tos_region,
        tos_endpoint=tos_endpoint,
        doubao_api_key=doubao_api_key,
        doubao_app_id=doubao_app_id,
        doubao_access_token=doubao_access_token,
        llm_api_key=llm_api_key,
        llm_model=llm_model,
        llm_base_url=llm_base_url,
    )
    try:
        config_path, env_path = write_initial_config(home, settings, force=force)
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(exc.exit_code) from exc
    typer.echo(f"Configuration: {config_path}")
    typer.echo(f"Secrets: {env_path}")
    if executor == "external_agent":
        typer.echo("Next: by2kb agent install hermes")
    else:
        typer.echo("Next: by2kb ingest <video-url>")


@enrichment_app.command("claim")
def enrichment_claim(
    job_id: str = typer.Argument(...),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    _configure_stdio()
    try:
        payload = claim_external_enrichment(load_config(), job_id)
    except By2kbError as exc:
        _command_error(exc, json_out)
    if json_out:
        typer.echo(json.dumps(payload, ensure_ascii=False))
    else:
        typer.echo(f"claimed enrichment task: {job_id}")


@enrichment_app.command("complete")
def enrichment_complete(
    job_id: str = typer.Argument(...),
    abstract_file: Path = typer.Option(..., "--abstract-file"),
    study_file: Path = typer.Option(..., "--study-file"),
    provider: str = typer.Option("external_agent", "--provider"),
    model: str = typer.Option("host-model", "--model"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    _configure_stdio()
    try:
        result = asyncio.run(
            complete_external_enrichment(
                load_config(),
                job_id,
                abstract_path=abstract_file,
                study_path=study_file,
                provider=provider,
                model=model,
            )
        )
    except By2kbError as exc:
        _command_error(exc, json_out)
    if json_out:
        typer.echo(json.dumps(result.to_dict(), ensure_ascii=False))
    else:
        typer.echo(f"completed enrichment task: {job_id}")


@enrichment_app.command("fail")
def enrichment_fail(
    job_id: str = typer.Argument(...),
    message: str = typer.Option(..., "--message"),
    retryable: bool = typer.Option(False, "--retryable"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    _configure_stdio()
    try:
        result = fail_external_enrichment(
            load_config(), job_id, message=message, retryable=retryable
        )
    except By2kbError as exc:
        _command_error(exc, json_out)
    if json_out:
        typer.echo(json.dumps(result.to_dict(), ensure_ascii=False))
    else:
        typer.echo(f"marked enrichment task {result.status}: {job_id}")


@agent_app.command("install")
def agent_install(
    host: str = typer.Argument(..., help="Agent host (currently: hermes)"),
    hermes_home: Path | None = typer.Option(None, "--hermes-home"),
    force: bool = typer.Option(False, "--force"),
    no_enable: bool = typer.Option(False, "--no-enable"),
) -> None:
    _configure_stdio()
    if host.lower() != "hermes":
        raise typer.BadParameter("supported agent host: hermes")
    try:
        target = install_hermes_plugin(
            hermes_home=hermes_home,
            force=force,
            enable=not no_enable,
        )
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(exc.exit_code) from exc
    typer.echo(f"Hermes plugin installed: {target}")
    typer.echo("Restart the Hermes gateway to activate it.")


def _command_error(exc: By2kbError, json_out: bool) -> None:
    if json_out:
        typer.echo(
            json.dumps(
                {"exit_code": exc.exit_code, "status": "error", "message": str(exc)},
                ensure_ascii=False,
            )
        )
    else:
        typer.echo(str(exc), err=True)
    raise typer.Exit(exc.exit_code)


if __name__ == "__main__":
    app()
