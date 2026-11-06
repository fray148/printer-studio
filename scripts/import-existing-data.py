#!/usr/bin/env python3
"""One-time, read-only snapshot of history/materials into a new fork's data."""
import json
import os
from pathlib import Path
import sqlite3
import sys

source,destination=map(Path,sys.argv[1:])
destination.mkdir(mode=0o700,parents=True,exist_ok=True)
for name in ('materials.db','history.json'):
    if (destination/name).exists():
        raise SystemExit('Destination already contains history/materials; refusing overwrite.')
database=source/'materials.db'
if database.is_file():
    original=sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True)
    copied=sqlite3.connect(destination/'materials.db')
    try:
        original.backup(copied)
    finally:
        copied.close();original.close()
    os.chmod(destination/'materials.db',0o600)
history=source/'history.json'
if history.is_file():
    contents=history.read_text()
    json.loads(contents)
    (destination/'history.json').write_text(contents)
    os.chmod(destination/'history.json',0o600)
print('Independent history/materials snapshot created.')
