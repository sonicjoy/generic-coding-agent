"""Console entry point for API and worker processes."""

from __future__ import annotations

import argparse
import signal
import threading

import uvicorn

from gca_service.app import create_app
from gca_service.config import ServiceSettings
from gca_service.events import structured_event
from gca_service.state import ServiceState
from gca_service.worker import ServiceWorker


def _serve(args: argparse.Namespace) -> int:
    settings = ServiceSettings.from_environment()
    state = ServiceState.build(settings)
    _emit(_startup_summary(settings, state))
    uvicorn.run(create_app(state=state), host=args.host, port=args.port, log_level=args.log_level)
    return 0


def _emit(message: str) -> None:
    """Write worker progress immediately (file redirects are block-buffered)."""

    print(message, flush=True)


def _worker(args: argparse.Namespace) -> int:
    settings = ServiceSettings.from_environment()
    _emit(
        structured_event(
            "worker",
            "start",
            worker_id=settings.worker_id,
            data_dir=settings.data_dir,
            lease_seconds=settings.lease_seconds,
            poll_seconds=settings.poll_seconds,
            once=bool(args.once),
        )
    )
    state = ServiceState.build(settings)
    _emit(
        structured_event(
            "worker",
            "store_ready",
            worker_id=settings.worker_id,
            data_dir=settings.data_dir,
        )
    )
    _emit(_startup_summary(settings, state))
    worker = ServiceWorker(state, on_event=_emit)
    if args.once:
        job = worker.run_once()
        if job is None:
            _emit("idle")
        else:
            line = f"{job.id} {job.status.value}"
            if job.last_error:
                line = f"{line}: {job.last_error}"
            _emit(line)
        return 0 if job is None or job.status.value == "completed" else 1
    stop = threading.Event()

    def _shutdown(signum: int, frame: object) -> None:
        _ = frame
        _emit(f"[worker] event=shutdown signal={signum}")
        worker.release_active_lease()
        stop.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    worker.run_forever(stop=stop)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the hosted-service CLI parser."""

    parser = argparse.ArgumentParser(prog="gca-service")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Run the authenticated HTTP API.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--log-level", default="info")
    serve.set_defaults(func=_serve)

    worker = commands.add_parser("worker", help="Run the asynchronous job worker.")
    worker.add_argument("--once", action="store_true", help="Process at most one job.")
    worker.set_defaults(func=_worker)
    return parser


def _startup_summary(settings: ServiceSettings, state: ServiceState) -> str:
    latest = state.store.list(limit=1)
    latest_job_id = latest[0].id if latest else "none"
    return f"gca-service data_dir={settings.data_dir} latest_job_id={latest_job_id}"


def main(argv: list[str] | None = None) -> int:
    """Run the service CLI."""

    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
