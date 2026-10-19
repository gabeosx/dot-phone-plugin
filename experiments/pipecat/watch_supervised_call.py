"""Read-only controller watcher for an explicitly approved, owned synthetic call.

The local marker coordinates the caller fixture; all answers and phone controls
come from the actual Dot or its delegated worker through authenticated MCP tools.
"""

import argparse
import json
import re
import subprocess
from pathlib import Path


def decision_was_forwarded(snapshot, pending):
    """A fast authenticated decision may finish between watcher samples."""
    return (
        snapshot["state"] == "active"
        and snapshot["progress"] == "Scoped answer forwarded; playback unconfirmed."
        and (pending is None or snapshot["revision"] == pending["revision"])
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("call_id")
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-f0-9]{32}", args.call_id):
        parser.error("exact prepared call ID required")
    if not args.directory.is_dir() or args.directory.is_symlink():
        parser.error("existing synthetic artifact directory required")
    script = """
import json,os,sqlite3,time
from dots_phone.config import Settings
from pathlib import Path
from cryptography.fernet import Fernet
v=json.loads(Path('/run/secrets/runtime').read_text())
os.environ.update(v)
settings=Settings.from_env()
f=Fernet(v['DOTS_PHONE_DATA_ENCRYPTION_KEY'].encode())
db=sqlite3.connect('file:/data/phone.sqlite3?mode=ro',uri=True)
call_id=CALL_ID
previous=None
# Include asynchronous Dot dispatch time before the separately bounded call.
for _ in range(30000):
 row=db.execute("SELECT payload FROM objects WHERE kind='call' AND id=?",(call_id,)).fetchone()
 c=json.loads(f.decrypt(row[0]))
 assert c['brief']['synthetic'] and c['brief']['destination'] in settings.allowed_numbers
 snapshot={k:c.get(k) for k in ('call_id','state','revision','provider_sid','pending','progress','stop_reason','usage','result','conclusion')}
 if snapshot['pending']:
  snapshot['pending']={k:snapshot['pending'][k] for k in ('request_id','revision','expires')}
 identity=json.dumps(snapshot,sort_keys=True)
 if identity!=previous:
  print(json.dumps({'observed_at_epoch':time.time(),**snapshot}),flush=True)
  previous=identity
 if c['state'] in {'completed','cancelled','failed'}:
  events=[]
  for r in db.execute('SELECT status,payload FROM deliveries'):
   e=json.loads(f.decrypt(r[1]))
   if e['data']['call_id']==call_id:
    events.append({'event_id':e['eventId'],'name':e['name'],'timestamp':e['timestamp'],'delivery_status':r[0]})
  print(json.dumps({'terminal_events':events}),flush=True)
  break
 time.sleep(0.02)
""".replace("CALL_ID", repr(args.call_id))
    process = subprocess.Popen(
        ["docker", "exec", "-i", "dots-phone-pipecat-phone-app-1", "python", "-u", "-"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    process.stdin.write(script)
    process.stdin.close()
    pending = None
    forwarded_requests = set()
    with (args.directory / "controller-observations.jsonl").open("a") as journal:
        for line in process.stdout:
            snapshot = json.loads(line)
            journal.write(line)
            journal.flush()
            if "terminal_events" in snapshot:
                (args.directory / "controller-events.json").write_text(
                    json.dumps(snapshot, indent=2) + "\n"
                )
                continue
            current_pending = snapshot.get("pending")
            if current_pending:
                pending = current_pending
            elif decision_was_forwarded(snapshot, pending):
                status = json.loads((args.directory / "owned-bot-status.json").read_text())
                assert len(status["calls"]) == 1
                receiver_sid, record = next(iter(status["calls"].items()))
                assert re.fullmatch(r"CA[0-9a-f]{32}", receiver_sid)
                mail_history = (
                    snapshot.get("usage", {}).get("local_mail_worker", {}).get("requests", [])
                )
                request_id = (
                    pending["request_id"]
                    if pending
                    else (mail_history[-1]["request_id"] if mail_history else None)
                )
                marker = {
                    "call_id": args.call_id,
                    "receiver_sid": receiver_sid,
                    "request_id": request_id,
                    "revision": snapshot["revision"],
                    "decision_forwarded": True,
                    "observed_at_epoch": snapshot["observed_at_epoch"],
                }
                path = args.directory / (receiver_sid + "-decision-forwarded.json")
                if record["state"] == "active" and request_id not in forwarded_requests:
                    temporary = path.with_suffix(".tmp")
                    temporary.write_text(json.dumps(marker, indent=2) + "\n")
                    temporary.replace(path)
                    forwarded_requests.add(request_id)
                context_history = (
                    snapshot.get("usage", {}).get("local_context_worker", {}).get("requests", [])
                )
                if record["state"] == "active" and context_history:
                    reads = [r for r in context_history if r["operation"] == "read"]
                    if reads:
                        read_marker = {
                            **marker,
                            "request_id": reads[-1]["request_id"],
                            "operation": "read",
                            "request_created_ms": reads[-1]["request_created_ms"],
                            "result_delivered_ms": reads[-1]["result_delivered_ms"],
                        }
                        read_path = args.directory / (receiver_sid + "-context-read.json")
                        temporary = read_path.with_suffix(".tmp")
                        temporary.write_text(json.dumps(read_marker, indent=2) + "\n")
                        temporary.replace(read_path)
                    if (snapshot.get("conclusion") or {}).get("outcome") == "completed":
                        confirmed_path = args.directory / (receiver_sid + "-hotel-confirmed.json")
                        temporary = confirmed_path.with_suffix(".tmp")
                        temporary.write_text(
                            json.dumps(
                                {
                                    **marker,
                                    "outcome": "completed",
                                    "conclusion": snapshot["conclusion"],
                                },
                                indent=2,
                            )
                            + "\n"
                        )
                        temporary.replace(confirmed_path)
                    elif (
                        context_history[-1]["operation"] == "complete"
                        and context_history[-1].get("completion_verified") is False
                    ):
                        retry_path = args.directory / (receiver_sid + "-confirmation-retry.json")
                        temporary = retry_path.with_suffix(".tmp")
                        temporary.write_text(json.dumps({**marker, **context_history[-1]}) + "\n")
                        temporary.replace(retry_path)
                pending = None
            if snapshot["state"] in {"completed", "cancelled", "failed"}:
                (args.directory / "controller-terminal.json").write_text(
                    json.dumps(snapshot, indent=2) + "\n"
                )
            print(
                json.dumps(
                    {k: snapshot.get(k) for k in ("state", "pending", "progress", "stop_reason")}
                ),
                flush=True,
            )
    if process.wait():
        raise SystemExit("read-only controller watcher failed")


if __name__ == "__main__":
    main()
