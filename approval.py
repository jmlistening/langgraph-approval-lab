import argparse
import json
import sqlite3
from pathlib import Path

from storage import ApprovalError, TicketStore
from workflow import ApprovalWorkflow


def nonempty(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("Expected a non-empty value.")
    return value


def main():
    parser = argparse.ArgumentParser(description="Run a local LangGraph ticket approval workflow.")
    parser.add_argument("--data-dir", type=Path, default=Path(".approval-data"))
    commands = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("create", "Create a ticket with a proposed change"),
        ("revise", "Edit a ticket and increment its revision"),
        ("show", "Show the ticket, applied text and application count"),
    ):
        subparser = commands.add_parser(name, help=help_text)
        subparser.add_argument("ticket_id", type=nonempty)
        if name != "show":
            subparser.add_argument("text", type=nonempty)

    request = commands.add_parser("request", help="Capture the current revision for review")
    request.add_argument("ticket_id", type=nonempty)
    request.add_argument("run_id", type=nonempty)
    decide = commands.add_parser("decide", help="Approve or reject the revision you reviewed")
    decide.add_argument("run_id", type=nonempty)
    decide.add_argument("revision", type=int)
    decide.add_argument("decision", choices=("approve", "reject"))

    for name, help_text in (
        ("status", "Show the status of an approval run"),
        ("recover", "Continue an interrupted execution after a crash"),
    ):
        subparser = commands.add_parser(name, help=help_text)
        subparser.add_argument("run_id", type=nonempty)

    args = parser.parse_args()
    try:
        args.data_dir.mkdir(parents=True, exist_ok=True)
        store = TicketStore(args.data_dir / "tickets.sqlite3")
        workflow = ApprovalWorkflow(args.data_dir, store)
        match args.command:
            case "create":
                result = store.create(args.ticket_id, args.text)
            case "revise":
                result = store.revise(args.ticket_id, args.text)
            case "show":
                result = store.get(args.ticket_id)
            case "request":
                result = workflow.request(args.ticket_id, args.run_id)
            case "decide":
                result = workflow.decide(args.run_id, args.revision, args.decision == "approve")
            case "status":
                result = workflow.status(args.run_id)
            case "recover":
                result = workflow.recover(args.run_id)
    except (ApprovalError, OSError, sqlite3.Error) as error:
        parser.exit(1, f"error: {error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
