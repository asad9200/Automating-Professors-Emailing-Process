"""Command-line entry points for long-running steps that should not block the UI."""

import argparse
import logging

from profreach.config import get_settings
from profreach.db.engine import get_engine, initialize_database
from profreach.db.outreach_store import OutreachStore
from profreach.db.profile_store import ProfileStore
from profreach.gmail.auth import GmailAuthError
from profreach.gmail.client import GmailError
from profreach.logging_config import configure_logging
from profreach.services import followup_agent, gmail_agent, gmail_client, outreach_runner

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="profreach")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("connect-gmail", help="Run the Gmail OAuth desktop consent flow")
    commands.add_parser("runs", help="List outreach runs and their stage")
    send = commands.add_parser("send", help="Send approved emails with throttling")
    send.add_argument(
        "--thread-id",
        help="Resume a paused pipeline run (see `profreach runs`); default sends all approved",
    )
    send.add_argument("--limit", type=int, default=None, help="Stop after this many sends")
    commands.add_parser("followups", help="Detect replies and draft follow-ups")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings)
    engine = get_engine(settings.database_url)
    initialize_database(engine)

    try:
        if args.command == "connect-gmail":
            address = gmail_client(settings, allow_browser_flow=True).my_address()
            print(f"Connected Gmail account: {address}")
        elif args.command == "runs":
            for run in OutreachStore(engine).list_runs():
                print(f"{run.thread_id}\t{run.stage}\t{len(run.professor_ids)} professors")
        elif args.command == "send" and args.thread_id:
            runner = outreach_runner(settings, engine, progress=print)
            if not runner.is_waiting_to_send(args.thread_id):
                print("This run is not waiting to send.")
                return 1
            state = runner.resume_send(args.thread_id, limit=args.limit, wait=True)
            print(f"Sent in this run so far: {len(state.get('sent', []))}")
        elif args.command == "send":
            report = gmail_agent(settings, engine).send_approved(
                limit=args.limit, wait=True, progress=print
            )
            print(f"Sent {len(report.sent)}; failed {len(report.failed)}.")
            if report.stopped_reason:
                print(report.stopped_reason)
        elif args.command == "followups":
            report = followup_agent(settings, engine).run(ProfileStore(engine).get_profile())
            print(
                f"Replies detected: {len(report.replied)}; "
                f"follow-ups drafted: {len(report.followups_drafted)}; "
                f"errors: {len(report.errors)}"
            )
    except (GmailAuthError, GmailError) as error:
        print(f"Gmail error: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
