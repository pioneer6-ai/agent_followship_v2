"""
Versioned database migrations for the scheduling system.

Provides:
- Forward migrations with automatic version tracking
- Rollback capability with integrity checks
- Schema inspection before migration
- Transaction-based execution with automatic rollback on failure
- Foreign key and index preservation
"""

import sqlite3
import json
from datetime import datetime, timezone
from typing import List, Dict, Optional, Tuple
from pathlib import Path


class MigrationError(Exception):
    """Raised when a migration fails."""
    pass


class Migration:
    """Base class for a database migration."""
    
    version: int = 0
    description: str = ""
    
    def up(self, conn: sqlite3.Connection):
        """Apply the migration."""
        raise NotImplementedError
    
    def down(self, conn: sqlite3.Connection):
        """Rollback the migration."""
        raise NotImplementedError
    
    def verify(self, conn: sqlite3.Connection) -> Tuple[bool, str]:
        """
        Verify the migration can be applied safely.
        Returns (can_apply, reason).
        """
        return True, ""


class MigrationRunner:
    """Manages and executes database migrations."""
    
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._ensure_migration_table()
    
    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        # Enable foreign key support
        conn.execute('PRAGMA foreign_keys = ON')
        return conn
    
    def _ensure_migration_table(self):
        """Create migrations tracking table if it doesn't exist."""
        with self._get_connection() as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    description TEXT NOT NULL,
                    applied_at TEXT NOT NULL,
                    applied_by TEXT,
                    rollback_available INTEGER NOT NULL DEFAULT 1
                )
            ''')
            conn.commit()
    
    def get_current_version(self) -> int:
        """Get the current schema version."""
        with self._get_connection() as conn:
            row = conn.execute(
                'SELECT MAX(version) as ver FROM schema_migrations'
            ).fetchone()
            return row['ver'] if row['ver'] is not None else 0
    
    def get_applied_migrations(self) -> List[Dict]:
        """Get list of applied migrations."""
        with self._get_connection() as conn:
            rows = conn.execute('''
                SELECT version, description, applied_at, applied_by
                FROM schema_migrations
                ORDER BY version
            ''').fetchall()
            return [dict(row) for row in rows]
    
    def inspect_schema(self) -> Dict:
        """
        Inspect current database schema.
        Returns tables, indexes, and foreign keys.
        """
        schema = {
            'tables': {},
            'indexes': [],
            'foreign_keys': {}
        }
        
        with self._get_connection() as conn:
            # Get tables
            tables = conn.execute("""
                SELECT name, sql FROM sqlite_master 
                WHERE type='table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name
            """).fetchall()
            
            for table_row in tables:
                table_name = table_row['name']
                schema['tables'][table_name] = {
                    'sql': table_row['sql'],
                    'columns': []
                }
                
                # Get columns
                columns = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
                schema['tables'][table_name]['columns'] = [dict(col) for col in columns]
                
                # Get foreign keys
                fks = conn.execute(f"PRAGMA foreign_key_list({table_name})").fetchall()
                if fks:
                    schema['foreign_keys'][table_name] = [dict(fk) for fk in fks]
            
            # Get indexes
            indexes = conn.execute("""
                SELECT name, tbl_name, sql FROM sqlite_master 
                WHERE type='index' AND sql IS NOT NULL
                ORDER BY name
            """).fetchall()
            schema['indexes'] = [dict(idx) for idx in indexes]
        
        return schema
    
    def apply_migration(self, migration: Migration, applied_by: str = 'system') -> bool:
        """
        Apply a migration within a transaction.
        Automatically rolls back on failure.
        """
        current_version = self.get_current_version()
        
        if migration.version <= current_version:
            raise MigrationError(
                f"Migration {migration.version} already applied (current: {current_version})"
            )
        
        # Verify migration can be applied
        conn = self._get_connection()
        try:
            can_apply, reason = migration.verify(conn)
            if not can_apply:
                raise MigrationError(f"Migration verification failed: {reason}")
            
            # Begin transaction
            conn.execute('BEGIN IMMEDIATE')
            
            # Apply migration
            migration.up(conn)
            
            # Record migration
            now = datetime.now(timezone.utc).isoformat()
            conn.execute('''
                INSERT INTO schema_migrations (version, description, applied_at, applied_by)
                VALUES (?, ?, ?, ?)
            ''', (migration.version, migration.description, now, applied_by))
            
            # Commit
            conn.commit()
            return True
            
        except Exception as e:
            conn.rollback()
            raise MigrationError(f"Migration {migration.version} failed: {e}") from e
        finally:
            conn.close()
    
    def rollback_migration(self, migration: Migration, applied_by: str = 'system') -> bool:
        """
        Rollback a migration within a transaction.
        """
        current_version = self.get_current_version()
        
        if migration.version != current_version:
            raise MigrationError(
                f"Can only rollback the latest migration (current: {current_version}, requested: {migration.version})"
            )
        
        conn = self._get_connection()
        try:
            # Temporarily disable foreign key checks for rollback
            conn.execute('PRAGMA foreign_keys = OFF')
            
            # Begin transaction
            conn.execute('BEGIN IMMEDIATE')
            
            # Rollback migration
            migration.down(conn)
            
            # Remove migration record
            conn.execute('DELETE FROM schema_migrations WHERE version = ?', (migration.version,))
            
            # Commit
            conn.commit()
            
            # Re-enable foreign keys
            conn.execute('PRAGMA foreign_keys = ON')
            return True
            
        except Exception as e:
            conn.rollback()
            conn.execute('PRAGMA foreign_keys = ON')
            raise MigrationError(f"Rollback {migration.version} failed: {e}") from e
        finally:
            conn.close()


# Example migration (not currently needed, but demonstrates the pattern)
class Migration001AddStaffColumn(Migration):
    """
    Example: Add a staff_notes column to appointment_requests.
    This is not currently needed but demonstrates the migration pattern.
    """
    version = 1
    description = "Add staff_notes column to appointment_requests"
    
    def verify(self, conn: sqlite3.Connection) -> Tuple[bool, str]:
        """Check if the column already exists."""
        cursor = conn.execute("PRAGMA table_info(appointment_requests)")
        columns = [row['name'] for row in cursor.fetchall()]
        
        if 'staff_notes' in columns:
            return False, "Column staff_notes already exists"
        
        return True, ""
    
    def up(self, conn: sqlite3.Connection):
        """Add the column."""
        conn.execute('''
            ALTER TABLE appointment_requests 
            ADD COLUMN staff_notes TEXT
        ''')
    
    def down(self, conn: sqlite3.Connection):
        """
        SQLite doesn't support DROP COLUMN directly.
        We'd need to recreate the table without the column.
        Foreign keys are disabled by the migration runner during rollback.
        """
        # Get current data
        conn.execute('''
            CREATE TABLE appointment_requests_backup AS 
            SELECT id, patient_id, patient_name, follow_up_case_id, slot_date, 
                   slot_session, slot_time, slot_datetime_utc, status, follow_up_reason,
                   requested_by, approved_by, approved_at, declined_reason, expires_at,
                   expired_at, created_at, updated_at
            FROM appointment_requests
        ''')
        
        # Drop and recreate
        conn.execute('DROP TABLE appointment_requests')
        conn.execute('''
            CREATE TABLE appointment_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                patient_id TEXT NOT NULL,
                patient_name TEXT NOT NULL,
                follow_up_case_id TEXT,
                slot_date TEXT NOT NULL,
                slot_session TEXT NOT NULL,
                slot_time TEXT NOT NULL,
                slot_datetime_utc TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                follow_up_reason TEXT,
                requested_by TEXT NOT NULL,
                approved_by TEXT,
                approved_at TEXT,
                declined_reason TEXT,
                expires_at TEXT,
                expired_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        ''')
        
        # Restore data
        conn.execute('''
            INSERT INTO appointment_requests 
            SELECT * FROM appointment_requests_backup
        ''')
        
        # Recreate indexes
        conn.execute('CREATE INDEX IF NOT EXISTS idx_appointments_status ON appointment_requests(status)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_appointments_patient ON appointment_requests(patient_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_appointments_slot ON appointment_requests(slot_datetime_utc)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_appointments_expires ON appointment_requests(expires_at)')
        
        # Clean up
        conn.execute('DROP TABLE appointment_requests_backup')


class Migration002AddSourceColumn(Migration):
    """
    Add a `source` column to appointment_requests, distinguishing
    patient-initiated bookings (Patient Portal) from staff-initiated ones
    (Staff Calendar), so a query/report can tell the two apart without
    inferring it from `follow_up_reason` text. Defaults existing rows to
    'staff' (the only source that existed before the Patient Portal wrote
    to this table), and new rows explicitly set it (see
    core/scheduling_calendar_adapter.py and web/calendar_routes.py).
    """
    version = 2
    description = "Add source column to appointment_requests"

    def verify(self, conn: sqlite3.Connection) -> Tuple[bool, str]:
        cursor = conn.execute("PRAGMA table_info(appointment_requests)")
        columns = [row['name'] for row in cursor.fetchall()]

        if 'source' in columns:
            return False, "Column source already exists"

        return True, ""

    def up(self, conn: sqlite3.Connection):
        conn.execute('''
            ALTER TABLE appointment_requests
            ADD COLUMN source TEXT NOT NULL DEFAULT 'staff'
        ''')
        conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_appointments_source
            ON appointment_requests(source)
        ''')

    def down(self, conn: sqlite3.Connection):
        conn.execute('''
            CREATE TABLE appointment_requests_backup AS
            SELECT id, patient_id, patient_name, follow_up_case_id, slot_date,
                   slot_session, slot_time, slot_datetime_utc, status, follow_up_reason,
                   requested_by, approved_by, approved_at, declined_reason, expires_at,
                   expired_at, created_at, updated_at
            FROM appointment_requests
        ''')

        conn.execute('DROP INDEX IF EXISTS idx_appointments_source')
        conn.execute('DROP TABLE appointment_requests')
        conn.execute('''
            CREATE TABLE appointment_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                patient_id TEXT NOT NULL,
                patient_name TEXT NOT NULL,
                follow_up_case_id TEXT,
                slot_date TEXT NOT NULL,
                slot_session TEXT NOT NULL,
                slot_time TEXT NOT NULL,
                slot_datetime_utc TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                follow_up_reason TEXT,
                requested_by TEXT NOT NULL,
                approved_by TEXT,
                approved_at TEXT,
                declined_reason TEXT,
                expires_at TEXT,
                expired_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        ''')

        conn.execute('''
            INSERT INTO appointment_requests
            SELECT * FROM appointment_requests_backup
        ''')

        conn.execute('CREATE INDEX IF NOT EXISTS idx_appointments_status ON appointment_requests(status)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_appointments_patient ON appointment_requests(patient_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_appointments_slot ON appointment_requests(slot_datetime_utc)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_appointments_expires ON appointment_requests(expires_at)')

        conn.execute('DROP TABLE appointment_requests_backup')


# Registry of all migrations
MIGRATIONS: List[Migration] = [
    # Migration001AddStaffColumn(),  # Example - not currently needed
    Migration002AddSourceColumn(),
]


def get_migration(version: int) -> Optional[Migration]:
    """Get a migration by version number."""
    for migration in MIGRATIONS:
        if migration.version == version:
            return migration
    return None
