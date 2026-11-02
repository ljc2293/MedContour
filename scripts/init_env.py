"""Create local configuration with random credentials; never overwrite .env."""
from pathlib import Path
import secrets


def main():
    root = Path(__file__).resolve().parents[1]
    target = root / ".env"
    template = (root / ".env.example").read_text(encoding="utf-8")
    for _ in range(2):
        template = template.replace("GENERATE_WITH_INIT_ENV", secrets.token_hex(24), 1)
    try:
        with target.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(template)
        target.chmod(0o600)
    except FileExistsError:
        raise SystemExit(".env already exists; it was not changed.")
    print("Created .env. Read INITIAL_ADMIN_EMAIL and INITIAL_ADMIN_PASSWORD locally before signing in.")


if __name__ == "__main__":
    main()
