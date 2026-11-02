"""Back up the database, saved annotations and task files from named volumes."""
from datetime import datetime, timezone
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[1]
stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
target = root / "backups" / stamp
target.mkdir(parents=True, exist_ok=False)
command = ["docker", "compose", "--project-directory", str(root)]
# Stop API writes during the snapshot; restore its prior running state afterward.
api_was_running = bool(subprocess.check_output(command + ["ps", "--status", "running", "-q", "api"], text=True).strip())
try:
    if api_was_running:
        subprocess.run(command + ["stop", "api"], check=True)
    dump = f"/backups/annotation-{stamp}.dump"
    subprocess.run(command + ["exec", "-T", "db", "pg_dump", "-U", "annotation", "-d", "annotation", "-Fc", "-f", dump], check=True)
    subprocess.run(command + ["cp", f"db:{dump}", str(target / "annotation.dump")], check=True)
    subprocess.run(command + ["cp", "api:/annotations", str(target / "annotations")], check=True)
    subprocess.run(command + ["cp", "api:/task-files", str(target / "task-files")], check=True)
finally:
    if api_was_running:
        subprocess.run(command + ["start", "api"], check=True)
print(f"Backup saved to {target}")
