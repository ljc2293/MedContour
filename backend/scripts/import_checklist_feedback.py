from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.checklist_importer import import_checklist_feedback_packages
from app.database import Base, SessionLocal, engine


def main() -> None:
    parser = argparse.ArgumentParser(description="Import independent Checklist feedback tasks")
    parser.add_argument("source", nargs="?", default="/checklist-source")
    args = parser.parse_args()

    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        result = import_checklist_feedback_packages(db, Path(args.source))
        db.commit()
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
