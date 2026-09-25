"""
SQLite persistence for email conversation threads and messages.

Provides durable storage and recovery for email conversations.
"""

import sqlite3
import json
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from contextlib import contextmanager

from agent.email_models import EmailThread, EmailMessage


class EmailPersistence:
    """
    Manages SQLite persistence for email threads and messages.
    
    Schema:
    - email_threads: thread metadata and flags
    - email_messages: individual messages in threads
    - processed_message_ids: replay deduplication tracking
    """
    
    def __init__(self, db_path: str = "email_conversations.db"):
        """
        Initialize email persistence.
        
        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = db_path
        self._initialize_schema()
    
    @contextmanager
    def _get_connection(self):
        """Context manager for database connections with transaction support."""
        conn = sqlite3.connect(self.db_path, isolation_level=None)  # Autocommit off
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN")  # Explicit transaction
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    
    def _initialize_schema(self):
        """Create database tables if they don't exist."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            # Email threads table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS email_threads (
                    thread_id TEXT PRIMARY KEY,
                    patient_id TEXT NOT NULL,
                    case_episode_id TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    staff_takeover INTEGER NOT NULL DEFAULT 0,
                    opted_out INTEGER NOT NULL DEFAULT 0,
                    needs_staff_review INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    last_activity TEXT NOT NULL
                )
            """)
            
            # Migrate existing databases to add needs_staff_review column
            cursor.execute("PRAGMA table_info(email_threads)")
            columns = [row[1] for row in cursor.fetchall()]
            if 'needs_staff_review' not in columns:
                cursor.execute("""
                    ALTER TABLE email_threads 
                    ADD COLUMN needs_staff_review INTEGER NOT NULL DEFAULT 0
                """)
            
            # Email messages table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS email_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_id TEXT NOT NULL,
                    message_id TEXT NOT NULL UNIQUE,
                    from_address TEXT NOT NULL,
                    to_address TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    body TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    headers TEXT NOT NULL,
                    FOREIGN KEY (thread_id) REFERENCES email_threads(thread_id)
                )
            """)
            
            # Processed message IDs for replay deduplication
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS processed_message_ids (
                    message_id TEXT PRIMARY KEY,
                    processed_at TEXT NOT NULL
                )
            """)
            
            # Unmatched emails requiring staff review
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS unmatched_emails (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT NOT NULL UNIQUE,
                    from_address TEXT NOT NULL,
                    to_address TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    body TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    headers TEXT NOT NULL,
                    failure_reason TEXT NOT NULL,
                    reviewed INTEGER NOT NULL DEFAULT 0,
                    reviewed_by TEXT,
                    reviewed_at TEXT,
                    resolution TEXT,
                    created_at TEXT NOT NULL
                )
            """)
            
            # Outbound conversation deliveries
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS outbound_deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_id TEXT NOT NULL,
                    outbound_message_id TEXT NOT NULL UNIQUE,
                    inbound_message_id TEXT NOT NULL,
                    to_address TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    body TEXT NOT NULL,
                    headers TEXT NOT NULL,
                    delivery_state TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    claimed_by TEXT,
                    claimed_at TEXT,
                    provider_message_id TEXT,
                    provider_result TEXT,
                    provider_error_code TEXT,
                    provider_error_message TEXT,
                    last_attempt_at TEXT,
                    next_retry_at TEXT,
                    completed_at TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (thread_id) REFERENCES email_threads(thread_id)
                )
            """)
            
            # Index for efficient thread lookups
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_messages_thread 
                ON email_messages(thread_id)
            """)
            
            # Index for delivery state queries
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_deliveries_state
                ON outbound_deliveries(delivery_state, next_retry_at)
            """)
            
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_messages_message_id 
                ON email_messages(message_id)
            """)
            
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_threads_patient 
                ON email_threads(patient_id)
            """)
    
    def save_thread(self, thread: EmailThread, conn=None) -> None:
        """
        Save or update an email thread.
        
        Args:
            thread: EmailThread to persist
            conn: Optional connection for transaction batching
        """
        def _save(cursor):
            cursor.execute("""
                INSERT OR REPLACE INTO email_threads 
                (thread_id, patient_id, case_episode_id, subject, 
                 staff_takeover, opted_out, needs_staff_review, created_at, last_activity)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                thread.thread_id,
                thread.patient_id,
                thread.case_episode_id,
                thread.subject,
                1 if thread.staff_takeover else 0,
                1 if thread.opted_out else 0,
                1 if thread.needs_staff_review else 0,
                thread.created_at.isoformat(),
                thread.last_activity.isoformat()
            ))
        
        if conn:
            _save(conn.cursor())
        else:
            with self._get_connection() as conn:
                _save(conn.cursor())
    
    def save_message(self, thread_id: str, message: EmailMessage, conn=None) -> None:
        """
        Save an email message.
        
        Args:
            thread_id: Thread this message belongs to
            message: EmailMessage to persist
            conn: Optional connection for transaction batching
        """
        def _save(cursor):
            cursor.execute("""
                INSERT OR IGNORE INTO email_messages
                (thread_id, message_id, from_address, to_address, subject,
                 body, timestamp, direction, headers)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                thread_id,
                message.message_id,
                message.from_address,
                message.to_address,
                message.subject,
                message.body,
                message.timestamp.isoformat(),
                message.direction,
                json.dumps(message.headers)
            ))
        
        if conn:
            _save(conn.cursor())
        else:
            with self._get_connection() as conn:
                _save(conn.cursor())
    
    def load_thread(self, thread_id: str) -> Optional[EmailThread]:
        """
        Load a thread with all its messages.
        
        Args:
            thread_id: Thread ID to load
            
        Returns:
            EmailThread if found, None otherwise
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            # Load thread metadata
            cursor.execute("""
                SELECT * FROM email_threads WHERE thread_id = ?
            """, (thread_id,))
            
            row = cursor.fetchone()
            if not row:
                return None
            
            # Create thread object
            thread = EmailThread(
                thread_id=row['thread_id'],
                patient_id=row['patient_id'],
                case_episode_id=row['case_episode_id'],
                subject=row['subject'],
                staff_takeover=bool(row['staff_takeover']),
                opted_out=bool(row['opted_out']),
                needs_staff_review=bool(row['needs_staff_review']),
                created_at=datetime.fromisoformat(row['created_at']),
                last_activity=datetime.fromisoformat(row['last_activity'])
            )
            
            # Load messages
            cursor.execute("""
                SELECT * FROM email_messages 
                WHERE thread_id = ? 
                ORDER BY timestamp ASC
            """, (thread_id,))
            
            for msg_row in cursor.fetchall():
                message = EmailMessage(
                    message_id=msg_row['message_id'],
                    from_address=msg_row['from_address'],
                    to_address=msg_row['to_address'],
                    subject=msg_row['subject'],
                    body=msg_row['body'],
                    timestamp=datetime.fromisoformat(msg_row['timestamp']),
                    direction=msg_row['direction'],
                    headers=json.loads(msg_row['headers'])
                )
                thread.messages.append(message)
            
            return thread
    
    def load_all_threads(self) -> Dict[str, EmailThread]:
        """
        Load all threads from database.
        
        Returns:
            Dict mapping thread_id to EmailThread
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute("SELECT thread_id FROM email_threads")
            thread_ids = [row['thread_id'] for row in cursor.fetchall()]
        
        threads = {}
        for thread_id in thread_ids:
            thread = self.load_thread(thread_id)
            if thread:
                threads[thread_id] = thread
        
        return threads
    
    def get_patient_threads(self, patient_id: str) -> List[EmailThread]:
        """
        Load all threads for a patient.
        
        Args:
            patient_id: Patient ID
            
        Returns:
            List of EmailThread objects
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT thread_id FROM email_threads 
                WHERE patient_id = ?
                ORDER BY last_activity DESC
            """, (patient_id,))
            
            thread_ids = [row['thread_id'] for row in cursor.fetchall()]
        
        threads = []
        for thread_id in thread_ids:
            thread = self.load_thread(thread_id)
            if thread:
                threads.append(thread)
        
        return threads
    
    def find_message_thread(self, message_id: str) -> Optional[str]:
        """
        Find which thread contains a given message ID.
        
        Args:
            message_id: Message ID to search for
            
        Returns:
            thread_id if found, None otherwise
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT thread_id FROM email_messages 
                WHERE message_id = ?
            """, (message_id,))
            
            row = cursor.fetchone()
            return row['thread_id'] if row else None
    
    def is_message_processed(self, message_id: str) -> bool:
        """
        Check if a message ID has already been processed (replay detection).
        
        Args:
            message_id: Message ID to check
            
        Returns:
            True if already processed
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT 1 FROM processed_message_ids 
                WHERE message_id = ?
            """, (message_id,))
            
            return cursor.fetchone() is not None
    
    def mark_message_processed(self, message_id: str, conn=None) -> None:
        """
        Mark a message ID as processed (replay prevention).
        
        Args:
            message_id: Message ID to mark
            conn: Optional connection for transaction batching
        """
        def _mark(cursor):
            cursor.execute("""
                INSERT OR IGNORE INTO processed_message_ids 
                (message_id, processed_at)
                VALUES (?, ?)
            """, (message_id, datetime.now(timezone.utc).isoformat()))
        
        if conn:
            _mark(conn.cursor())
        else:
            with self._get_connection() as conn:
                _mark(conn.cursor())
    
    def get_thread_count(self) -> int:
        """Get total number of threads."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM email_threads")
            return cursor.fetchone()[0]
    
    def get_message_count(self) -> int:
        """Get total number of messages."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM email_messages")
            return cursor.fetchone()[0]

    def save_unmatched_email(
        self,
        message_id: str,
        from_address: str,
        to_address: str,
        subject: str,
        body: str,
        timestamp: datetime,
        headers: Dict[str, str],
        failure_reason: str
    ) -> int:
        """
        Save an unmatched email for staff review.
        
        Args:
            message_id: Unique Message-ID
            from_address: Sender email
            to_address: Recipient email
            subject: Email subject
            body: Email body
            timestamp: When email was received
            headers: Email headers
            failure_reason: Why patient matching failed
            
        Returns:
            Database ID of saved unmatched email
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute("""
                INSERT INTO unmatched_emails
                (message_id, from_address, to_address, subject, body,
                 timestamp, headers, failure_reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                message_id,
                from_address,
                to_address,
                subject,
                body,
                timestamp.isoformat(),
                json.dumps(headers),
                failure_reason,
                datetime.now(timezone.utc).isoformat()
            ))
            
            return cursor.lastrowid
    
    def is_unmatched_email_processed(self, message_id: str) -> bool:
        """
        Check if an unmatched email was already saved (replay detection).
        
        Args:
            message_id: Message ID to check
            
        Returns:
            True if already saved, False otherwise
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COUNT(*) FROM unmatched_emails 
                WHERE message_id = ?
            """, (message_id,))
            return cursor.fetchone()[0] > 0
    
    def get_unmatched_emails(
        self,
        include_reviewed: bool = False,
        limit: Optional[int] = None
    ) -> List[dict]:
        """
        Retrieve unmatched emails for staff review.
        
        Args:
            include_reviewed: Include already-reviewed emails
            limit: Maximum number to return
            
        Returns:
            List of unmatched email records as dicts
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            query = """
                SELECT * FROM unmatched_emails
            """
            
            if not include_reviewed:
                query += " WHERE reviewed = 0"
            
            query += " ORDER BY created_at DESC"
            
            if limit:
                query += f" LIMIT {limit}"
            
            cursor.execute(query)
            
            results = []
            for row in cursor.fetchall():
                results.append({
                    'id': row['id'],
                    'message_id': row['message_id'],
                    'from_address': row['from_address'],
                    'to_address': row['to_address'],
                    'subject': row['subject'],
                    'body': row['body'],
                    'timestamp': datetime.fromisoformat(row['timestamp']),
                    'headers': json.loads(row['headers']),
                    'failure_reason': row['failure_reason'],
                    'reviewed': bool(row['reviewed']),
                    'reviewed_by': row['reviewed_by'],
                    'reviewed_at': datetime.fromisoformat(row['reviewed_at']) if row['reviewed_at'] else None,
                    'resolution': row['resolution'],
                    'created_at': datetime.fromisoformat(row['created_at'])
                })
            
            return results
    
    def mark_unmatched_email_reviewed(
        self,
        unmatched_id: int,
        staff_user_id: str,
        resolution: str
    ) -> bool:
        """
        Mark an unmatched email as reviewed by staff.
        
        Args:
            unmatched_id: Database ID of unmatched email
            staff_user_id: ID of staff member who reviewed
            resolution: Resolution action taken
            
        Returns:
            True if updated successfully
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute("""
                UPDATE unmatched_emails
                SET reviewed = 1,
                    reviewed_by = ?,
                    reviewed_at = ?,
                    resolution = ?
                WHERE id = ?
            """, (
                staff_user_id,
                datetime.now(timezone.utc).isoformat(),
                resolution,
                unmatched_id
            ))
            
            return cursor.rowcount > 0
    
    def get_unmatched_email_count(self, include_reviewed: bool = False) -> int:
        """
        Get count of unmatched emails.
        
        Args:
            include_reviewed: Include already-reviewed emails
            
        Returns:
            Count of unmatched emails
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            query = "SELECT COUNT(*) FROM unmatched_emails"
            if not include_reviewed:
                query += " WHERE reviewed = 0"
            
            cursor.execute(query)
            return cursor.fetchone()[0]

    def save_escalation_atomic(
        self,
        thread: EmailThread,
        thread_id: str,
        inbound_message: EmailMessage,
        message_id: str
    ) -> None:
        """
        Atomically save escalation state, inbound message, and processing marker.
        
        All three operations occur in a single transaction. If any fails, all roll back.
        
        Args:
            thread: Thread with needs_staff_review=True
            thread_id: Thread ID
            inbound_message: Inbound message to save
            message_id: Message ID to mark as processed
        """
        with self._get_connection() as conn:
            # All three operations share this connection and commit together
            self.save_thread(thread, conn=conn)
            self.save_message(thread_id, inbound_message, conn=conn)
            self.mark_message_processed(message_id, conn=conn)
            # Commit happens when context manager exits successfully
    
    def save_unmatched_atomic(
        self,
        message_id: str,
        from_address: str,
        to_address: str,
        subject: str,
        body: str,
        timestamp: datetime,
        headers: Dict[str, str],
        failure_reason: str
    ) -> int:
        """
        Atomically save unmatched email and mark as processed.
        
        Args:
            message_id: Unique Message-ID
            from_address: Sender email
            to_address: Recipient email
            subject: Email subject
            body: Email body
            timestamp: When email was received
            headers: Email headers
            failure_reason: Why patient matching failed
            
        Returns:
            Database ID of saved unmatched email
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            # Save unmatched email
            cursor.execute("""
                INSERT INTO unmatched_emails
                (message_id, from_address, to_address, subject, body,
                 timestamp, headers, failure_reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                message_id,
                from_address,
                to_address,
                subject,
                body,
                timestamp.isoformat(),
                json.dumps(headers),
                failure_reason,
                datetime.now(timezone.utc).isoformat()
            ))
            
            unmatched_id = cursor.lastrowid
            
            # Mark as processed
            self.mark_message_processed(message_id, conn=conn)
            
            return unmatched_id

    def create_outbound_delivery(
        self,
        thread_id: str,
        outbound_message_id: str,
        inbound_message_id: str,
        to_address: str,
        subject: str,
        body: str,
        headers: Dict[str, str]
    ) -> int:
        """
        Create pending outbound delivery record before sending.
        
        Args:
            thread_id: Thread ID
            outbound_message_id: Outbound message ID
            inbound_message_id: Inbound message being replied to
            to_address: Recipient email
            subject: Email subject
            body: Email body
            headers: Email headers
            
        Returns:
            Delivery ID
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute("""
                INSERT INTO outbound_deliveries
                (thread_id, outbound_message_id, inbound_message_id, to_address,
                 subject, body, headers, delivery_state, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """, (
                thread_id,
                outbound_message_id,
                inbound_message_id,
                to_address,
                subject,
                body,
                json.dumps(headers),
                datetime.now(timezone.utc).isoformat()
            ))
            
            return cursor.lastrowid
    
    def claim_delivery(self, delivery_id: int, processor_id: str) -> bool:
        """
        Atomically claim a delivery for processing.
        
        Args:
            delivery_id: Delivery ID to claim
            processor_id: Processor identifier
            
        Returns:
            True if claimed successfully, False if already claimed
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            # Atomic claim: only succeeds if not already claimed
            cursor.execute("""
                UPDATE outbound_deliveries
                SET claimed_by = ?,
                    claimed_at = ?,
                    delivery_state = 'claimed'
                WHERE id = ?
                  AND claimed_by IS NULL
                  AND delivery_state = 'pending'
            """, (
                processor_id,
                datetime.now(timezone.utc).isoformat(),
                delivery_id
            ))
            
            return cursor.rowcount > 0
    
    def record_delivery_attempt(
        self,
        delivery_id: int,
        provider_outcome: Dict[str, Any],
        next_retry_at: Optional[datetime] = None
    ) -> None:
        """
        Record delivery attempt result.
        
        Args:
            delivery_id: Delivery ID
            provider_outcome: Provider outcome dict (success, message_id, error_code, etc.)
            next_retry_at: When to retry (None if terminal)
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            success = provider_outcome.get('success', False)
            
            if success:
                # Successful delivery
                cursor.execute("""
                    UPDATE outbound_deliveries
                    SET attempt_count = attempt_count + 1,
                        delivery_state = 'delivered',
                        provider_message_id = ?,
                        provider_result = ?,
                        last_attempt_at = ?,
                        completed_at = ?
                    WHERE id = ?
                """, (
                    provider_outcome.get('message_id'),
                    json.dumps(provider_outcome),
                    datetime.now(timezone.utc).isoformat(),
                    datetime.now(timezone.utc).isoformat(),
                    delivery_id
                ))
            elif next_retry_at:
                # Retryable failure
                cursor.execute("""
                    UPDATE outbound_deliveries
                    SET attempt_count = attempt_count + 1,
                        delivery_state = 'pending',
                        provider_error_code = ?,
                        provider_error_message = ?,
                        provider_result = ?,
                        last_attempt_at = ?,
                        next_retry_at = ?,
                        claimed_by = NULL,
                        claimed_at = NULL
                    WHERE id = ?
                """, (
                    provider_outcome.get('error_code'),
                    provider_outcome.get('error_message'),
                    json.dumps(provider_outcome),
                    datetime.now(timezone.utc).isoformat(),
                    next_retry_at.isoformat(),
                    delivery_id
                ))
            else:
                # Terminal failure
                cursor.execute("""
                    UPDATE outbound_deliveries
                    SET attempt_count = attempt_count + 1,
                        delivery_state = 'failed',
                        provider_error_code = ?,
                        provider_error_message = ?,
                        provider_result = ?,
                        last_attempt_at = ?,
                        completed_at = ?
                    WHERE id = ?
                """, (
                    provider_outcome.get('error_code'),
                    provider_outcome.get('error_message'),
                    json.dumps(provider_outcome),
                    datetime.now(timezone.utc).isoformat(),
                    datetime.now(timezone.utc).isoformat(),
                    delivery_id
                ))
    
    def mark_delivery_needs_review(
        self,
        delivery_id: int,
        reason: str,
        provider_outcome: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Mark delivery as needing staff review (uncertain outcome).
        
        Args:
            delivery_id: Delivery ID
            reason: Why review is needed
            provider_outcome: Optional provider response to preserve
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            # Serialize provider outcome if provided
            import json
            provider_result = json.dumps(provider_outcome) if provider_outcome else None
            
            cursor.execute("""
                UPDATE outbound_deliveries
                SET delivery_state = 'needs_review',
                    provider_error_message = ?,
                    provider_result = ?
                WHERE id = ?
            """, (reason, provider_result, delivery_id))
    
    def get_pending_deliveries(
        self,
        limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Get pending deliveries ready to send.
        
        Args:
            limit: Maximum number to return
            
        Returns:
            List of delivery records
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            now = datetime.now(timezone.utc).isoformat()
            
            query = """
                SELECT * FROM outbound_deliveries
                WHERE delivery_state = 'pending'
                  AND (next_retry_at IS NULL OR next_retry_at <= ?)
                  AND claimed_by IS NULL
                ORDER BY created_at ASC
            """
            
            if limit:
                query += f" LIMIT {limit}"
            
            cursor.execute(query, (now,))
            
            results = []
            for row in cursor.fetchall():
                results.append({
                    'id': row['id'],
                    'thread_id': row['thread_id'],
                    'outbound_message_id': row['outbound_message_id'],
                    'inbound_message_id': row['inbound_message_id'],
                    'to_address': row['to_address'],
                    'subject': row['subject'],
                    'body': row['body'],
                    'headers': json.loads(row['headers']),
                    'attempt_count': row['attempt_count'],
                    'next_retry_at': datetime.fromisoformat(row['next_retry_at']) if row['next_retry_at'] else None,
                    'created_at': datetime.fromisoformat(row['created_at'])
                })
            
            return results
    
    def get_delivery_by_outbound_message_id(
        self,
        outbound_message_id: str
    ) -> Optional[Dict[str, Any]]:
        """
        Get delivery record by outbound message ID.
        
        Args:
            outbound_message_id: Outbound message ID
            
        Returns:
            Delivery record or None
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT * FROM outbound_deliveries
                WHERE outbound_message_id = ?
            """, (outbound_message_id,))
            
            row = cursor.fetchone()
            if not row:
                return None
            
            return {
                'id': row['id'],
                'thread_id': row['thread_id'],
                'outbound_message_id': row['outbound_message_id'],
                'inbound_message_id': row['inbound_message_id'],
                'delivery_state': row['delivery_state'],
                'attempt_count': row['attempt_count'],
                'provider_message_id': row['provider_message_id'],
                'provider_error_code': row['provider_error_code']
            }
