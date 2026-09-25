"""
Gmail IMAP poller for inbound conversation emails.

Polls a dedicated Gmail mailbox folder for new messages and feeds them
to the email conversation manager. Uses IMAP UID-based checkpointing
to avoid reprocessing historical messages and ensure reliable restart recovery.

Authentication: Gmail App Password (not OAuth)
"""

import imaplib
import email
from email.message import Message as EmailMessage
from email.header import decode_header
from typing import Optional, Dict, List, Tuple
import sqlite3
import json
from datetime import datetime, timezone
from pathlib import Path
import time


class ImapCheckpoint:
    """
    Persistent IMAP checkpoint storage using SQLite.
    
    Stores UIDVALIDITY and last processed UID per folder to ensure
    reliable restart recovery and avoid reprocessing messages.
    Also provides quarantine storage for messages that fail processing.
    """
    
    def __init__(self, db_path: str):
        """
        Initialize checkpoint storage.
        
        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = db_path
        self._init_schema()
    
    def _init_schema(self):
        """Create checkpoint and quarantine tables if not exist."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS imap_checkpoint (
                    folder TEXT PRIMARY KEY,
                    uidvalidity INTEGER NOT NULL,
                    last_uid INTEGER NOT NULL,
                    last_updated TEXT NOT NULL,
                    paused INTEGER NOT NULL DEFAULT 0,
                    pause_reason TEXT
                )
            """)
            
            # Quarantine table for messages that fail processing
            conn.execute("""
                CREATE TABLE IF NOT EXISTS imap_quarantine (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    folder TEXT NOT NULL,
                    uidvalidity INTEGER NOT NULL,
                    uid INTEGER NOT NULL,
                    raw_message BLOB NOT NULL,
                    failure_reason TEXT NOT NULL,
                    error_details TEXT,
                    quarantined_at TEXT NOT NULL,
                    UNIQUE(folder, uidvalidity, uid)
                )
            """)
            conn.commit()
    
    def get_checkpoint(self, folder: str) -> Optional[Tuple[int, int]]:
        """
        Get checkpoint for a folder.
        
        Args:
            folder: IMAP folder name
            
        Returns:
            (uidvalidity, last_uid) tuple, or None if no checkpoint exists
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT uidvalidity, last_uid, paused, pause_reason FROM imap_checkpoint WHERE folder = ?",
                (folder,)
            )
            row = cursor.fetchone()
            if row:
                return (row[0], row[1], bool(row[2]), row[3])
        return None
    
    def save_checkpoint(self, folder: str, uidvalidity: int, last_uid: int, paused: bool = False, pause_reason: str = None):
        """
        Save checkpoint for a folder.
        
        Args:
            folder: IMAP folder name
            uidvalidity: Current UIDVALIDITY value
            last_uid: Last successfully processed UID
            paused: Whether polling is paused (requires operator intervention)
            pause_reason: Reason for pause (e.g., "UIDVALIDITY changed")
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO imap_checkpoint (folder, uidvalidity, last_uid, last_updated, paused, pause_reason)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (folder, uidvalidity, last_uid, datetime.now(timezone.utc).isoformat(), 1 if paused else 0, pause_reason))
            conn.commit()
    
    def unpause_checkpoint(self, folder: str) -> bool:
        """
        Unpause a paused checkpoint (operator recovery action).
        
        Args:
            folder: IMAP folder name
            
        Returns:
            True if checkpoint was unpaused, False if not found or not paused
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT paused FROM imap_checkpoint WHERE folder = ?",
                (folder,)
            )
            row = cursor.fetchone()
            if not row or not row[0]:
                return False
            
            conn.execute("""
                UPDATE imap_checkpoint 
                SET paused = 0, pause_reason = NULL, last_updated = ?
                WHERE folder = ?
            """, (datetime.now(timezone.utc).isoformat(), folder))
            conn.commit()
            return True
    
    def quarantine_message(
        self,
        folder: str,
        uidvalidity: int,
        uid: int,
        raw_message: bytes,
        failure_reason: str,
        error_details: str = ""
    ) -> bool:
        """
        Store a message in quarantine for later review.
        
        Args:
            folder: IMAP folder name
            uidvalidity: Current UIDVALIDITY value
            uid: Message UID
            raw_message: Raw RFC822 message bytes
            failure_reason: Short reason code (e.g., "parse_error", "ingestion_failed")
            error_details: Detailed error message/traceback
            
        Returns:
            True if quarantine succeeded, False otherwise
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO imap_quarantine 
                    (folder, uidvalidity, uid, raw_message, failure_reason, error_details, quarantined_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    folder,
                    uidvalidity,
                    uid,
                    raw_message,
                    failure_reason,
                    error_details,
                    datetime.now(timezone.utc).isoformat()
                ))
                conn.commit()
            return True
        except Exception as e:
            print(f"[IMAP Checkpoint] Quarantine failed: {e}")
            return False


class GmailImapPoller:
    """
    Gmail IMAP poller for inbound conversation emails.
    
    Connects to Gmail via IMAP using App Password authentication,
    polls a dedicated folder for new messages, and feeds them to
    the email conversation manager.
    
    Features:
    - UID-based checkpointing (no reprocessing historical messages)
    - UIDVALIDITY tracking (detects mailbox resets)
    - Message-ID deduplication (handled by conversation manager)
    - Safe body parsing (text/plain extraction)
    - Preserves all email headers
    - Does not delete messages or rely on unread status
    """
    
    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        folder: str,
        checkpoint_db: str,
        use_ssl: bool = True
    ):
        """
        Initialize Gmail IMAP poller.
        
        Args:
            host: IMAP server hostname (imap.gmail.com)
            port: IMAP port (993 for SSL, 143 for STARTTLS)
            username: Gmail email address
            password: Gmail App Password (16 chars with spaces)
            folder: IMAP folder to poll (e.g., "INBOX/Conversations")
            checkpoint_db: Path to checkpoint database
            use_ssl: Whether to use SSL (default True for Gmail)
        """
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.folder = folder
        self.use_ssl = use_ssl
        self.checkpoint = ImapCheckpoint(checkpoint_db)
        self._conn: Optional[imaplib.IMAP4_SSL] = None
    
    def connect(self) -> bool:
        """
        Connect to Gmail IMAP server.
        
        Returns:
            True if connection successful, False otherwise
        """
        try:
            if self.use_ssl:
                self._conn = imaplib.IMAP4_SSL(self.host, self.port)
            else:
                self._conn = imaplib.IMAP4(self.host, self.port)
                self._conn.starttls()
            
            # Login with App Password
            self._conn.login(self.username, self.password)
            return True
            
        except Exception as e:
            print(f"[IMAP Poller] Connection failed: {e}")
            return False
    
    def disconnect(self):
        """Disconnect from IMAP server."""
        if self._conn:
            try:
                self._conn.logout()
            except:
                pass
            self._conn = None
    
    def _select_folder(self) -> bool:
        """
        Select the configured folder.
        
        Returns:
            True if folder selected successfully, False otherwise
        """
        try:
            status, _ = self._conn.select(self.folder, readonly=False)
            return status == 'OK'
        except Exception as e:
            print(f"[IMAP Poller] Folder selection failed: {e}")
            return False
    
    def _get_uidvalidity(self) -> Optional[int]:
        """
        Get current UIDVALIDITY for selected folder.
        
        Returns:
            UIDVALIDITY value, or None if unavailable
        """
        try:
            status, response = self._conn.status(self.folder, '(UIDVALIDITY)')
            if status == 'OK':
                # Parse response: b'INBOX (UIDVALIDITY 1234)'
                import re
                match = re.search(r'UIDVALIDITY (\d+)', response[0].decode())
                if match:
                    return int(match.group(1))
        except:
            pass
        return None
    
    def _decode_header(self, header_value: str) -> str:
        """
        Decode email header safely.
        
        Args:
            header_value: Raw header value
            
        Returns:
            Decoded string
        """
        if not header_value:
            return ""
        
        decoded_parts = []
        for part, encoding in decode_header(header_value):
            if isinstance(part, bytes):
                decoded_parts.append(part.decode(encoding or 'utf-8', errors='replace'))
            else:
                decoded_parts.append(str(part))
        return ' '.join(decoded_parts)
    
    def _extract_text_body(self, msg: EmailMessage) -> str:
        """
        Extract plain text body from email message.
        
        Args:
            msg: Email message object
            
        Returns:
            Plain text body
        """
        body = ""
        
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                if content_type == 'text/plain':
                    try:
                        payload = part.get_payload(decode=True)
                        charset = part.get_content_charset() or 'utf-8'
                        body = payload.decode(charset, errors='replace')
                        break
                    except:
                        continue
        else:
            try:
                payload = msg.get_payload(decode=True)
                charset = msg.get_content_charset() or 'utf-8'
                body = payload.decode(charset, errors='replace')
            except:
                body = str(msg.get_payload())
        
        return body.strip()
    
    def _parse_message(self, raw_msg: bytes) -> Optional[Dict]:
        """
        Parse raw email message.
        
        Args:
            raw_msg: Raw email bytes
            
        Returns:
            Dict with parsed message fields, or None if parsing fails
        """
        try:
            msg = email.message_from_bytes(raw_msg)
            
            # Extract headers
            from_addr = self._decode_header(msg.get('From', ''))
            # Extract just the email address from "Name <email@example.com>"
            import re
            from_match = re.search(r'<(.+?)>', from_addr)
            from_address = from_match.group(1) if from_match else from_addr.split()[-1]
            
            to_addr = self._decode_header(msg.get('To', ''))
            to_match = re.search(r'<(.+?)>', to_addr)
            to_address = to_match.group(1) if to_match else to_addr.split()[-1] if to_addr else ""
            
            subject = self._decode_header(msg.get('Subject', ''))
            message_id = msg.get('Message-ID', '').strip('<>')
            in_reply_to = msg.get('In-Reply-To', '').strip('<>')
            references = msg.get('References', '')
            
            # Extract body
            body = self._extract_text_body(msg)
            
            # Preserve important headers
            headers = {}
            if in_reply_to:
                headers['In-Reply-To'] = f'<{in_reply_to}>'
            if references:
                headers['References'] = references
            
            # Check for auto-reply headers (to avoid processing auto-replies)
            auto_submitted = msg.get('Auto-Submitted', '').lower()
            x_autoreply = msg.get('X-Autoreply', '').lower()
            is_auto_reply = auto_submitted in ('auto-replied', 'auto-generated') or x_autoreply == 'yes'
            
            return {
                'from_address': from_address,
                'to_address': to_address,
                'subject': subject,
                'body': body,
                'message_id': message_id,
                'headers': headers,
                'is_auto_reply': is_auto_reply,
                'raw_message': msg
            }
            
        except Exception as e:
            print(f"[IMAP Poller] Message parsing failed: {e}")
            return None
    
    def fetch_new_messages(self, start_uid: int = 1) -> List[Tuple[int, bytes, Optional[Dict]]]:
        """
        Fetch new messages since last checkpoint.
        
        Args:
            start_uid: UID to start fetching from (default 1)
            
        Returns:
            List of (uid, raw_message_bytes, parsed_message) tuples
            parsed_message is None if parsing failed
        """
        if not self._conn:
            return []
        
        messages = []
        
        try:
            # Search for messages with UID >= start_uid
            status, response = self._conn.uid('SEARCH', None, f'UID {start_uid}:*')
            if status != 'OK':
                return []
            
            uid_list = response[0].decode().split()
            if not uid_list:
                return []
            
            # Fetch each message
            for uid_str in uid_list:
                uid = int(uid_str)
                
                # Fetch message
                status, response = self._conn.uid('FETCH', uid_str, '(RFC822)')
                if status != 'OK' or not response or not response[0]:
                    continue
                
                raw_msg = response[0][1]
                parsed = self._parse_message(raw_msg)
                
                # Include message even if parsing failed (for quarantine)
                messages.append((uid, raw_msg, parsed))
            
        except Exception as e:
            print(f"[IMAP Poller] Fetch failed: {e}")
        
        return messages
    
    def poll(
        self,
        conversation_manager,
        patient_lookup: Dict,
        case_lookup: Dict
    ) -> Dict[str, int]:
        """
        Poll for new messages and process them.
        
        Checkpoint is advanced only after:
        - Successful durable ingestion (result.success=True)
        - Successful quarantine of poison messages
        
        Checkpoint is NOT advanced on transient failures - processing stops
        and the failed UID remains pending for next poll.
        
        Args:
            conversation_manager: Email conversation manager
            patient_lookup: Dict mapping email addresses to patients
            case_lookup: Dict mapping patient IDs to cases
            
        Returns:
            Statistics dict with counts
        """
        stats = {
            'fetched': 0,
            'processed': 0,
            'skipped': 0,
            'quarantined': 0,
            'failed_transient': 0
        }
        
        if not self.connect():
            print("[IMAP Poller] Connection failed")
            return stats
        
        try:
            if not self._select_folder():
                print(f"[IMAP Poller] Folder '{self.folder}' not accessible")
                return stats
            
            # Get current UIDVALIDITY
            current_uidvalidity = self._get_uidvalidity()
            if not current_uidvalidity:
                print("[IMAP Poller] Could not get UIDVALIDITY")
                return stats
            
            # Get checkpoint
            checkpoint = self.checkpoint.get_checkpoint(self.folder)
            
            if checkpoint:
                saved_uidvalidity, last_uid, paused, pause_reason = checkpoint
                
                # Check if polling is paused (requires operator intervention)
                if paused:
                    print(f"[IMAP Poller] Polling paused: {pause_reason}")
                    print("[IMAP Poller] Operator intervention required - call unpause_checkpoint()")
                    return stats
                
                # Check if mailbox was reset
                if saved_uidvalidity != current_uidvalidity:
                    print(f"[IMAP Poller] UIDVALIDITY changed ({saved_uidvalidity} -> {current_uidvalidity})")
                    print("[IMAP Poller] Mailbox was reset - pausing polling")
                    print("[IMAP Poller] Operator intervention required to resume")
                    # Pause checkpoint with new UIDVALIDITY
                    self.checkpoint.save_checkpoint(
                        self.folder, 
                        current_uidvalidity, 
                        0, 
                        paused=True,
                        pause_reason=f"UIDVALIDITY changed from {saved_uidvalidity} to {current_uidvalidity}"
                    )
                    return stats
                else:
                    # Continue from last processed + 1
                    start_uid = last_uid + 1
            else:
                # First run - establish checkpoint at current latest without processing
                status, response = self._conn.uid('SEARCH', None, 'ALL')
                if status == 'OK' and response[0]:
                    all_uids = response[0].decode().split()
                    if all_uids:
                        latest_uid = int(all_uids[-1])
                        print(f"[IMAP Poller] First run - checkpoint at UID {latest_uid} (history skipped)")
                        self.checkpoint.save_checkpoint(self.folder, current_uidvalidity, latest_uid)
                        return stats
                    else:
                        # Empty mailbox - set checkpoint to 0
                        print("[IMAP Poller] First run - empty mailbox, checkpoint at UID 0")
                        self.checkpoint.save_checkpoint(self.folder, current_uidvalidity, 0)
                        return stats
                else:
                    # Could not determine latest UID - set to 0 and wait for next poll
                    print("[IMAP Poller] First run - could not determine latest UID, checkpoint at UID 0")
                    self.checkpoint.save_checkpoint(self.folder, current_uidvalidity, 0)
                    return stats
            
            # Fetch new messages
            messages = self.fetch_new_messages(start_uid)
            stats['fetched'] = len(messages)
            
            if not messages:
                print(f"[IMAP Poller] No new messages (from UID {start_uid})")
                return stats
            
            print(f"[IMAP Poller] Fetched {len(messages)} new message(s)")
            
            # Process each message sequentially
            # Stop on first transient failure (don't advance checkpoint)
            for uid, raw_msg, parsed in messages:
                # If parsing failed, quarantine and advance
                if parsed is None:
                    print(f"[IMAP Poller] Parse failed for UID {uid} - quarantining")
                    quarantine_ok = self.checkpoint.quarantine_message(
                        folder=self.folder,
                        uidvalidity=current_uidvalidity,
                        uid=uid,
                        raw_message=raw_msg,
                        failure_reason="parse_error",
                        error_details="Message parsing failed"
                    )
                    
                    if quarantine_ok:
                        stats['quarantined'] += 1
                        # Advance checkpoint after successful quarantine
                        self.checkpoint.save_checkpoint(self.folder, current_uidvalidity, uid)
                        print(f"[IMAP Poller] UID {uid} quarantined, checkpoint advanced")
                    else:
                        # Quarantine failed - stop processing (transient failure)
                        print(f"[IMAP Poller] Quarantine failed for UID {uid} - stopping")
                        stats['failed_transient'] += 1
                        break
                    continue
                
                # Skip auto-replies (but advance checkpoint - these are confirmed skips)
                if parsed.get('is_auto_reply'):
                    print(f"[IMAP Poller] Skipping auto-reply: UID {uid}")
                    stats['skipped'] += 1
                    self.checkpoint.save_checkpoint(self.folder, current_uidvalidity, uid)
                    continue
                
                try:
                    # Feed to conversation manager
                    result = conversation_manager.receive_email(
                        from_address=parsed['from_address'],
                        to_address=parsed['to_address'],
                        subject=parsed['subject'],
                        body=parsed['body'],
                        message_id=parsed['message_id'],
                        headers=parsed['headers'],
                        patient_lookup=patient_lookup,
                        case_lookup=case_lookup
                    )
                    
                    if result.success:
                        # Success includes:
                        # - Durable ingestion with reply generation
                        # - Confirmed duplicates (already persisted)
                        # - Escalations (durably persisted)
                        # - Opt-outs (durably persisted)
                        stats['processed'] += 1
                        self.checkpoint.save_checkpoint(self.folder, current_uidvalidity, uid)
                        print(f"[IMAP Poller] UID {uid} processed: {result.reason}")
                    else:
                        # Ingestion failed (e.g., unknown sender)
                        # Unknown sender failures are already handled by conversation manager
                        # (saved to unmatched queue), so treat as permanent and quarantine
                        print(f"[IMAP Poller] Ingestion failed for UID {uid}: {result.reason}")
                        print(f"[IMAP Poller] Quarantining UID {uid} as permanent failure")
                        
                        quarantine_ok = self.checkpoint.quarantine_message(
                            folder=self.folder,
                            uidvalidity=current_uidvalidity,
                            uid=uid,
                            raw_message=raw_msg,
                            failure_reason="ingestion_failed",
                            error_details=result.reason
                        )
                        
                        if quarantine_ok:
                            stats['quarantined'] += 1
                            self.checkpoint.save_checkpoint(self.folder, current_uidvalidity, uid)
                            print(f"[IMAP Poller] UID {uid} quarantined, checkpoint advanced")
                        else:
                            # Quarantine failed - stop processing
                            print(f"[IMAP Poller] Quarantine failed for UID {uid} - stopping")
                            stats['failed_transient'] += 1
                            break
                    
                except Exception as e:
                    # Exception during ingestion - likely transient (DB connection, etc.)
                    # Leave UID pending and stop processing
                    print(f"[IMAP Poller] Exception processing UID {uid}: {e}")
                    print(f"[IMAP Poller] Leaving UID {uid} pending for retry")
                    stats['failed_transient'] += 1
                    # Do NOT advance checkpoint - message will be retried on next poll
                    break
            
        finally:
            self.disconnect()
        
        return stats
