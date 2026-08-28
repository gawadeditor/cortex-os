"""
tests/test_prod_tripwire.py -- proves the production-DB WRITE tripwire
(tests/_prod_tripwire.py, armed in conftest.pytest_configure) fires on
writes and allows reads.
"""
from __future__ import annotations

import sqlite3
import subprocess

import pytest

from tests._prod_tripwire import ProductionAccessError, prod_db_path

_PROD = prod_db_path()


class TestBlocksWrites:

    def test_insert_into_production_is_blocked(self):
        conn = sqlite3.connect(str(_PROD))
        with pytest.raises(ProductionAccessError):
            conn.execute("INSERT INTO runs (run_id) VALUES ('x')")

    def test_update_production_is_blocked(self):
        conn = sqlite3.connect(str(_PROD))
        with pytest.raises(ProductionAccessError):
            conn.execute("UPDATE channel_operations_profiles SET weekly_video_budget_usd = 1")

    def test_delete_production_is_blocked(self):
        conn = sqlite3.connect(str(_PROD))
        with pytest.raises(ProductionAccessError):
            conn.execute("DELETE FROM runs")

    def test_commit_on_production_is_a_silent_noop(self):
        # every DML was already blocked at execute(); commit has nothing
        # to flush, so it is a no-op rather than an error (defensive read
        # paths that call commit() after only SELECTs must not break).
        conn = sqlite3.connect(str(_PROD))
        assert conn.commit() is None

    def test_executescript_with_a_real_write_is_blocked(self):
        conn = sqlite3.connect(str(_PROD))
        with pytest.raises(ProductionAccessError):
            conn.executescript("CREATE TABLE IF NOT EXISTS z (a); INSERT INTO z VALUES (1);")

    def test_executescript_idempotent_ddl_is_allowed(self):
        conn = sqlite3.connect(str(_PROD))
        conn.executescript("CREATE TABLE IF NOT EXISTS runs (run_id TEXT);")  # no-op

    def test_get_connection_none_fallthrough_to_prod_blocks_writes_allows_reads(self):
        import hypogeum.db as hdb

        conn = hdb.get_connection(None)   # no binding -> _DEFAULT_DB_PATH == production
        # reads are fine
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] >= 0
        # writes are not
        with pytest.raises(ProductionAccessError):
            conn.execute("INSERT INTO runs (run_id) VALUES ('x')")

    def test_subprocess_argv_referencing_production_is_blocked(self):
        with pytest.raises(ProductionAccessError):
            subprocess.run(["sqlite3", str(_PROD), "DELETE FROM runs"])


class TestAllowsReads:

    def test_select_from_production_is_allowed(self):
        conn = sqlite3.connect(f"file:{_PROD}?mode=ro", uri=True)
        n = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        assert isinstance(n, int)
        conn.close()

    def test_pragma_read_is_allowed(self):
        conn = sqlite3.connect(str(_PROD))
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        conn.close()

    def test_temp_db_writes_are_allowed(self, tmp_path):
        conn = sqlite3.connect(str(tmp_path / "ok.db"))
        conn.execute("CREATE TABLE t (x)")
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
        conn.close()

    def test_in_memory_db_is_allowed(self):
        sqlite3.connect(":memory:").execute("CREATE TABLE t (x)")
