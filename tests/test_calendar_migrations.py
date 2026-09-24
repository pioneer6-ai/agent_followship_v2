"""
Pytest tests for database migrations.
"""

import pytest
from pathlib import Path
from datetime import datetime, timezone

from scheduling.database import SchedulingDatabase
from scheduling.migrations import MigrationRunner, Migration001AddStaffColumn


@pytest.fixture
def test_db(tmp_path):
    """Create temporary database for migration testing."""
    db_path = tmp_path / 'migration_test.db'
    db = SchedulingDatabase(str(db_path))
    
    # Add test data
    now = datetime.now(timezone.utc)
    test_id = db.create_appointment_request(
        patient_id='M001',
        patient_name='Migration Test Patient',
        slot_date='2026-11-01',
        slot_session='morning',
        slot_time='10:00',
        slot_datetime_utc=now.isoformat(),
        requested_by='test',
        expires_at=now.isoformat()
    )
    
    db._test_id = test_id
    yield db


@pytest.fixture
def runner(test_db):
    """Create migration runner."""
    return MigrationRunner(test_db.db_path)


@pytest.fixture
def migration():
    """Create migration instance."""
    return Migration001AddStaffColumn()


class TestMigrations:
    """Test migration system."""
    
    def test_forward_migration(self, runner, migration, test_db):
        """Test forward migration applies successfully."""
        runner.apply_migration(migration, 'test_user')
        
        # Verify data preserved
        appt = test_db.get_appointment(test_db._test_id)
        assert appt is not None
        assert appt['patient_name'] == 'Migration Test Patient'
    
    def test_repeat_protection(self, runner, migration):
        """Test migration cannot be applied twice."""
        runner.apply_migration(migration, 'test_user')
        
        with pytest.raises(Exception) as exc_info:
            runner.apply_migration(migration, 'test_user')
        
        assert 'already applied' in str(exc_info.value)
    
    def test_rollback(self, runner, migration, test_db):
        """Test migration rollback."""
        runner.apply_migration(migration, 'test_user')
        runner.rollback_migration(migration, 'test_user')
        
        # Verify data still preserved
        appt = test_db.get_appointment(test_db._test_id)
        assert appt is not None
        assert appt['patient_name'] == 'Migration Test Patient'
    
    def test_migration_tracking(self, runner, migration):
        """Test migration history is tracked."""
        runner.apply_migration(migration, 'test_user')
        
        import sqlite3
        conn = sqlite3.connect(runner.db_path)
        conn.row_factory = sqlite3.Row
        
        history = conn.execute(
            'SELECT * FROM schema_migrations WHERE version=?',
            (migration.version,)
        ).fetchone()
        conn.close()
        
        assert history is not None
        assert history['applied_by'] == 'test_user'
