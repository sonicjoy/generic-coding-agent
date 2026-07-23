"""Console entry point for API and worker processes."""

from __future__ import annotations

import argparse

import uvicorn

from gca_service.app import create_app
from gca_service.config import ServiceSettings
from gca_service.state import ServiceState
from gca_service.worker import ServiceWorker


def _serve(args: argparse.Namespace) -> int:
    settings = ServiceSettings.from_environment()
    uvicorn.run(create_app(settings), host=args.host, port=args.port, log_level=args.log_level)
    return 0


def _emit(message: str) -> None:
    """Write worker progress immediately (file redirects are block-buffered)."""

    print(message, flush=True)


def _worker(args: argparse.Namespace) -> int:
    settings = ServiceSettings.from_environment()
    state = ServiceState.build(settings)
    worker = ServiceWorker(state, on_event=_emit)
    if args.once:
        job = worker.run_once()
        _emit("idle" if job is None else f"{job.id} {job.status.value}")
        return 0 if job is None or job.status.value == "completed" else 1
    worker.run_forever()
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


def main(argv: list[str] | None = None) -> int:
    """Run the service CLI."""

    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
