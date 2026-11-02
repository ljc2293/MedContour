"""Show service health and aggregate case status without patient identifiers."""
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[1]
command = ["docker", "compose", "--project-directory", str(root)]
subprocess.run(command + ["ps", "-a"], check=True)
subprocess.run(command + ["exec", "-T", "db", "psql", "-U", "annotation", "-d", "annotation", "-c",
    "SELECT p.name, pc.status, count(*) FROM project_cases pc JOIN projects p ON p.id=pc.project_id "
    "GROUP BY p.name, pc.status ORDER BY p.name, pc.status;"], check=True)
