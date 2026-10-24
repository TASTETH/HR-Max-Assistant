"""Convenience launcher for disposable native PostgreSQL tests; no secret in history."""
import argparse
import getpass
import os
import runpy
from pathlib import Path
from psycopg.conninfo import make_conninfo

p=argparse.ArgumentParser()
p.add_argument('--allow-write',action='store_true',help='Confirm the selected DB is disposable')
args=p.parse_args()
if not args.allow_write:
    p.error('--allow-write is required for fixture inserts into the disposable test DB')
os.environ['KORSIK_TEST_DSN']=make_conninfo(
    host=os.getenv('PGHOST','127.0.0.1'),port=int(os.getenv('PGPORT','55432')),
    dbname=os.getenv('PGDATABASE','korsik_test'),user=os.getenv('PGUSER','korsik_app'),
    password=getpass.getpass('Password for the test database role: '),sslmode=os.getenv('PGSSLMODE','disable'))
os.environ['KORSIK_TEST_ALLOW_WRITE']='1'
runpy.run_path(str(Path(__file__).with_name('concurrency.py')),run_name='__main__')
