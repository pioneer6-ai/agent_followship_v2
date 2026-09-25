"""
Tests for Gmail IMAP poller with correct checkpoint behavior.

Tests cover:
- Checkpoint persistence and recovery
- Quarantine storage for failed messages
- UIDVALIDITY change detection (no auto-processing)
- Checkpoint advancement only after durable storage
- Transient failure handling (stop processing, don't advance)
- Restart safety after failures
"""

import pytest
import sqlite3
import tempfile
from datetime import datetime, timezone
from unittest.mock import Mock, MagicMock, patch
from email.message import EmailMessage

from agent.imap_poller import ImapCheckpoint, GmailImapPoller


@pytest.fixture
def temp_checkpoint_db():
    """Create temporary checkpoint database."""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        yield f.name


def test_checkpoint_initial_state(temp_checkpoint_db):
    """Checkpoint returns None for folder with no saved state."""
    checkpoint = ImapCheckpoint(temp_checkpoint_db)
    
    result = checkpoint.get_checkpoint('INBOX')
    assert result is None


def test_checkpoint_save_and_retrieve(temp_checkpoint_db):
    """Checkpoint can be saved and retrieved."""
    checkpoint = ImapCheckpoint(temp_checkpoint_db)
    
    checkpoint.save_checkpoint('INBOX', uidvalidity=12345, last_uid=100)
    
    result = checkpoint.get_checkpoint('INBOX')
    assert result == (12345, 100, False, None)  # Not paused


def test_checkpoint_update_existing(temp_checkpoint_db):
    """Checkpoint can be updated for same folder."""
    checkpoint = ImapCheckpoint(temp_checkpoint_db)
    
    checkpoint.save_checkpoint('INBOX', uidvalidity=12345, last_uid=100)
    checkpoint.save_checkpoint('INBOX', uidvalidity=12345, last_uid=150)
    
    result = checkpoint.get_checkpoint('INBOX')
    assert result == (12345, 150, False, None)


def test_checkpoint_multiple_folders(temp_checkpoint_db):
    """Different folders have independent checkpoints."""
    checkpoint = ImapCheckpoint(temp_checkpoint_db)
    
    checkpoint.save_checkpoint('INBOX', uidvalidity=111, last_uid=100)
    checkpoint.save_checkpoint('INBOX/Test', uidvalidity=222, last_uid=200)
    
    inbox_result = checkpoint.get_checkpoint('INBOX')
    test_result = checkpoint.get_checkpoint('INBOX/Test')
    
    assert inbox_result == (111, 100, False, None)
    assert test_result == (222, 200, False, None)


def test_checkpoint_paused_state(temp_checkpoint_db):
    """Checkpoint can be saved in paused state."""
    checkpoint = ImapCheckpoint(temp_checkpoint_db)
    
    checkpoint.save_checkpoint(
        'INBOX', 
        uidvalidity=12345, 
        last_uid=100,
        paused=True,
        pause_reason="UIDVALIDITY changed"
    )
    
    result = checkpoint.get_checkpoint('INBOX')
    assert result == (12345, 100, True, "UIDVALIDITY changed")


def test_checkpoint_unpause(temp_checkpoint_db):
    """Paused checkpoint can be unpaused."""
    checkpoint = ImapCheckpoint(temp_checkpoint_db)
    
    # Pause
    checkpoint.save_checkpoint(
        'INBOX',
        uidvalidity=12345,
        last_uid=100,
        paused=True,
        pause_reason="Test pause"
    )
    
    # Verify paused
    result = checkpoint.get_checkpoint('INBOX')
    assert result[2] is True  # paused
    
    # Unpause
    success = checkpoint.unpause_checkpoint('INBOX')
    assert success is True
    
    # Verify unpaused
    result = checkpoint.get_checkpoint('INBOX')
    assert result[2] is False  # not paused
    assert result[3] is None  # no pause reason


def test_quarantine_message(temp_checkpoint_db):
    """Messages can be quarantined with failure details."""
    checkpoint = ImapCheckpoint(temp_checkpoint_db)
    
    raw_msg = b"From: test@example.com\r\nSubject: Test\r\n\r\nBody"
    
    result = checkpoint.quarantine_message(
        folder='INBOX',
        uidvalidity=12345,
        uid=101,
        raw_message=raw_msg,
        failure_reason='parse_error',
        error_details='Invalid header encoding'
    )
    
    assert result is True
    
    # Verify stored in database
    with sqlite3.connect(temp_checkpoint_db) as conn:
        cursor = conn.execute(
            "SELECT uid, failure_reason, error_details FROM imap_quarantine WHERE folder = ?",
            ('INBOX',)
        )
        row = cursor.fetchone()
        assert row[0] == 101
        assert row[1] == 'parse_error'
        assert row[2] == 'Invalid header encoding'


def test_quarantine_duplicate_uid_replaces(temp_checkpoint_db):
    """Quarantining same UID twice replaces previous entry."""
    checkpoint = ImapCheckpoint(temp_checkpoint_db)
    
    raw_msg1 = b"First message"
    raw_msg2 = b"Second message"
    
    checkpoint.quarantine_message('INBOX', 12345, 101, raw_msg1, 'reason1')
    checkpoint.quarantine_message('INBOX', 12345, 101, raw_msg2, 'reason2')
    
    # Should only have one entry
    with sqlite3.connect(temp_checkpoint_db) as conn:
        cursor = conn.execute("SELECT COUNT(*) FROM imap_quarantine WHERE uid = 101")
        count = cursor.fetchone()[0]
        assert count == 1
        
        cursor = conn.execute("SELECT failure_reason FROM imap_quarantine WHERE uid = 101")
        reason = cursor.fetchone()[0]
        assert reason == 'reason2'  # Latest


def create_mock_email(
    from_addr="patient@example.com",
    to_addr="clinic@example.com",
    subject="Test",
    body="Test message",
    message_id="msg123@example.com",
    in_reply_to=None,
    auto_reply=False
):
    """Create mock email message."""
    msg = EmailMessage()
    msg['From'] = from_addr
    msg['To'] = to_addr
    msg['Subject'] = subject
    msg['Message-ID'] = f'<{message_id}>'
    
    if in_reply_to:
        msg['In-Reply-To'] = f'<{in_reply_to}>'
        msg['References'] = f'<{in_reply_to}>'
    
    if auto_reply:
        msg['Auto-Submitted'] = 'auto-replied'
    
    msg.set_content(body)
    
    return msg.as_bytes()


@patch('agent.imap_poller.imaplib.IMAP4_SSL')
def test_poller_first_run_skips_history(MockIMAP, temp_checkpoint_db):
    """First run establishes checkpoint without processing history."""
    mock_conn = MagicMock()
    MockIMAP.return_value = mock_conn
    
    mock_conn.login.return_value = ('OK', [])
    mock_conn.select.return_value = ('OK', [])
    mock_conn.status.return_value = ('OK', [b'INBOX (UIDVALIDITY 12345)'])
    mock_conn.uid.return_value = ('OK', [b'1 50 100 150 200'])  # 5 historical messages
    mock_conn.logout.return_value = ('OK', [])
    
    poller = GmailImapPoller(
        host='imap.gmail.com',
        port=993,
        username='test@gmail.com',
        password='app-password',
        folder='INBOX',
        checkpoint_db=temp_checkpoint_db,
        use_ssl=True
    )
    
    mock_conversation_manager = Mock()
    patient_lookup = {}
    case_lookup = {}
    
    stats = poller.poll(mock_conversation_manager, patient_lookup, case_lookup)
    
    # Should not fetch or process any messages (first run)
    assert stats['fetched'] == 0
    assert stats['processed'] == 0
    
    # Should establish checkpoint at latest UID
    checkpoint = ImapCheckpoint(temp_checkpoint_db)
    result = checkpoint.get_checkpoint('INBOX')
    assert result[0] == 12345  # UIDVALIDITY
    assert result[1] == 200    # Latest UID
    assert result[2] is False  # Not paused


@patch('agent.imap_poller.imaplib.IMAP4_SSL')
def test_poller_uidvalidity_change_pauses_polling(MockIMAP, temp_checkpoint_db):
    """UIDVALIDITY change pauses polling persistently."""
    # Old checkpoint
    checkpoint = ImapCheckpoint(temp_checkpoint_db)
    checkpoint.save_checkpoint('INBOX', uidvalidity=12345, last_uid=100)
    
    mock_conn = MagicMock()
    MockIMAP.return_value = mock_conn
    
    mock_conn.login.return_value = ('OK', [])
    mock_conn.select.return_value = ('OK', [])
    mock_conn.status.return_value = ('OK', [b'INBOX (UIDVALIDITY 99999)'])  # Changed!
    mock_conn.logout.return_value = ('OK', [])
    
    poller = GmailImapPoller(
        host='imap.gmail.com',
        port=993,
        username='test@gmail.com',
        password='app-password',
        folder='INBOX',
        checkpoint_db=temp_checkpoint_db,
        use_ssl=True
    )
    
    mock_conversation_manager = Mock()
    stats = poller.poll(mock_conversation_manager, {}, {})
    
    # Should NOT process any messages
    assert stats['fetched'] == 0
    assert stats['processed'] == 0
    
    # Checkpoint should be PAUSED with new UIDVALIDITY
    result = checkpoint.get_checkpoint('INBOX')
    assert result[0] == 99999  # New UIDVALIDITY
    assert result[1] == 0  # Reset to UID 0
    assert result[2] is True  # Paused
    assert "UIDVALIDITY changed" in result[3]  # Pause reason
    
    # Conversation manager should NOT be called
    mock_conversation_manager.receive_email.assert_not_called()


@patch('agent.imap_poller.imaplib.IMAP4_SSL')
def test_poller_paused_state_persists_across_polls(MockIMAP, temp_checkpoint_db):
    """Paused state persists - second poll also refuses to process."""
    # Create paused checkpoint
    checkpoint = ImapCheckpoint(temp_checkpoint_db)
    checkpoint.save_checkpoint(
        'INBOX',
        uidvalidity=99999,
        last_uid=0,
        paused=True,
        pause_reason="UIDVALIDITY changed from 12345 to 99999"
    )
    
    mock_conn = MagicMock()
    MockIMAP.return_value = mock_conn
    
    mock_conn.login.return_value = ('OK', [])
    mock_conn.select.return_value = ('OK', [])
    mock_conn.status.return_value = ('OK', [b'INBOX (UIDVALIDITY 99999)'])
    # Messages available but should not be fetched
    mock_conn.uid.return_value = ('OK', [b'1 2 3'])
    mock_conn.logout.return_value = ('OK', [])
    
    poller = GmailImapPoller(
        host='imap.gmail.com',
        port=993,
        username='test@gmail.com',
        password='app-password',
        folder='INBOX',
        checkpoint_db=temp_checkpoint_db,
        use_ssl=True
    )
    
    mock_conversation_manager = Mock()
    stats = poller.poll(mock_conversation_manager, {}, {})
    
    # Should refuse to process
    assert stats['fetched'] == 0
    assert stats['processed'] == 0
    
    # Checkpoint should still be paused
    result = checkpoint.get_checkpoint('INBOX')
    assert result[2] is True  # Still paused


@patch('agent.imap_poller.imaplib.IMAP4_SSL')
def test_poller_resumes_after_unpause(MockIMAP, temp_checkpoint_db):
    """After operator unpauses, polling resumes normally."""
    # Create paused checkpoint
    checkpoint = ImapCheckpoint(temp_checkpoint_db)
    checkpoint.save_checkpoint(
        'INBOX',
        uidvalidity=99999,
        last_uid=0,
        paused=True,
        pause_reason="UIDVALIDITY changed"
    )
    
    # Operator unpauses
    success = checkpoint.unpause_checkpoint('INBOX')
    assert success is True
    
    mock_conn = MagicMock()
    MockIMAP.return_value = mock_conn
    
    mock_conn.login.return_value = ('OK', [])
    mock_conn.select.return_value = ('OK', [])
    mock_conn.status.return_value = ('OK', [b'INBOX (UIDVALIDITY 99999)'])
    
    mock_conn.uid.side_effect = [
        ('OK', [b'1']),  # Search result
        ('OK', [(b'1 (RFC822 {123}', create_mock_email())])  # Fetch result
    ]
    mock_conn.logout.return_value = ('OK', [])
    
    poller = GmailImapPoller(
        host='imap.gmail.com',
        port=993,
        username='test@gmail.com',
        password='app-password',
        folder='INBOX',
        checkpoint_db=temp_checkpoint_db,
        use_ssl=True
    )
    
    mock_conversation_manager = Mock()
    mock_conversation_manager.receive_email.return_value = Mock(success=True)
    
    stats = poller.poll(mock_conversation_manager, {}, {})
    
    # Should process normally
    assert stats['fetched'] == 1
    assert stats['processed'] == 1


@patch('agent.imap_poller.imaplib.IMAP4_SSL')
def test_poller_processes_successfully(MockIMAP, temp_checkpoint_db):
    """Successful processing advances checkpoint."""
    checkpoint = ImapCheckpoint(temp_checkpoint_db)
    checkpoint.save_checkpoint('INBOX', uidvalidity=12345, last_uid=100)
    
    mock_conn = MagicMock()
    MockIMAP.return_value = mock_conn
    
    mock_conn.login.return_value = ('OK', [])
    mock_conn.select.return_value = ('OK', [])
    mock_conn.status.return_value = ('OK', [b'INBOX (UIDVALIDITY 12345)'])
    
    mock_conn.uid.side_effect = [
        ('OK', [b'101']),  # Search result
        ('OK', [(b'101 (RFC822 {123}', create_mock_email())])  # Fetch result
    ]
    mock_conn.logout.return_value = ('OK', [])
    
    poller = GmailImapPoller(
        host='imap.gmail.com',
        port=993,
        username='test@gmail.com',
        password='app-password',
        folder='INBOX',
        checkpoint_db=temp_checkpoint_db,
        use_ssl=True
    )
    
    mock_conversation_manager = Mock()
    mock_conversation_manager.receive_email.return_value = Mock(success=True, reason="Processed")
    
    stats = poller.poll(mock_conversation_manager, {}, {})
    
    assert stats['fetched'] == 1
    assert stats['processed'] == 1
    assert stats['quarantined'] == 0
    assert stats['failed_transient'] == 0
    
    # Checkpoint should advance
    result = checkpoint.get_checkpoint('INBOX')
    assert result[0] == 12345
    assert result[1] == 101


@patch('agent.imap_poller.imaplib.IMAP4_SSL')
def test_poller_ingestion_failure_quarantines_and_advances(MockIMAP, temp_checkpoint_db):
    """Ingestion failure (success=False) quarantines message and advances checkpoint."""
    checkpoint = ImapCheckpoint(temp_checkpoint_db)
    checkpoint.save_checkpoint('INBOX', uidvalidity=12345, last_uid=100)
    
    mock_conn = MagicMock()
    MockIMAP.return_value = mock_conn
    
    mock_conn.login.return_value = ('OK', [])
    mock_conn.select.return_value = ('OK', [])
    mock_conn.status.return_value = ('OK', [b'INBOX (UIDVALIDITY 12345)'])
    
    mock_conn.uid.side_effect = [
        ('OK', [b'101']),
        ('OK', [(b'101 (RFC822 {123}', create_mock_email())])
    ]
    mock_conn.logout.return_value = ('OK', [])
    
    poller = GmailImapPoller(
        host='imap.gmail.com',
        port=993,
        username='test@gmail.com',
        password='app-password',
        folder='INBOX',
        checkpoint_db=temp_checkpoint_db,
        use_ssl=True
    )
    
    # Conversation manager returns success=False (e.g., unknown sender)
    mock_conversation_manager = Mock()
    mock_conversation_manager.receive_email.return_value = Mock(
        success=False,
        reason="Unknown sender: unknown@example.com"
    )
    
    stats = poller.poll(mock_conversation_manager, {}, {})
    
    assert stats['fetched'] == 1
    assert stats['processed'] == 0
    assert stats['quarantined'] == 1  # Message quarantined
    assert stats['failed_transient'] == 0
    
    # Checkpoint should advance after quarantine
    result = checkpoint.get_checkpoint('INBOX')
    assert result[0] == 12345  # uidvalidity
    assert result[1] == 101    # last_uid
    
    # Verify quarantine record exists
    with sqlite3.connect(temp_checkpoint_db) as conn:
        cursor = conn.execute("SELECT COUNT(*) FROM imap_quarantine WHERE uid = 101")
        count = cursor.fetchone()[0]
        assert count == 1


@patch('agent.imap_poller.imaplib.IMAP4_SSL')
def test_poller_exception_leaves_uid_pending(MockIMAP, temp_checkpoint_db):
    """Exception during processing leaves UID pending (no quarantine, no advance)."""
    checkpoint = ImapCheckpoint(temp_checkpoint_db)
    checkpoint.save_checkpoint('INBOX', uidvalidity=12345, last_uid=100)
    
    mock_conn = MagicMock()
    MockIMAP.return_value = mock_conn
    
    mock_conn.login.return_value = ('OK', [])
    mock_conn.select.return_value = ('OK', [])
    mock_conn.status.return_value = ('OK', [b'INBOX (UIDVALIDITY 12345)'])
    
    # Two messages: 101 and 102
    mock_conn.uid.side_effect = [
        ('OK', [b'101 102']),  # Search
        ('OK', [(b'101 (RFC822 {123}', create_mock_email())]),  # Fetch 101
        ('OK', [(b'102 (RFC822 {123}', create_mock_email())]),  # Fetch 102
    ]
    mock_conn.logout.return_value = ('OK', [])
    
    poller = GmailImapPoller(
        host='imap.gmail.com',
        port=993,
        username='test@gmail.com',
        password='app-password',
        folder='INBOX',
        checkpoint_db=temp_checkpoint_db,
        use_ssl=True
    )
    
    # Conversation manager raises exception (transient failure)
    mock_conversation_manager = Mock()
    mock_conversation_manager.receive_email.side_effect = Exception("Database connection lost")
    
    stats = poller.poll(mock_conversation_manager, {}, {})
    
    assert stats['fetched'] == 2  # Both fetched
    assert stats['processed'] == 0
    assert stats['quarantined'] == 0  # NOT quarantined
    assert stats['failed_transient'] == 1
    
    # Checkpoint should NOT advance (still at 100)
    result = checkpoint.get_checkpoint('INBOX')
    assert result[0] == 12345
    assert result[1] == 100  # Not advanced
    
    # Only first message processing attempted (stopped after exception)
    assert mock_conversation_manager.receive_email.call_count == 1


@patch('agent.imap_poller.imaplib.IMAP4_SSL')
def test_poller_transient_failure_then_retry_succeeds(MockIMAP, temp_checkpoint_db):
    """After transient failure, retry on next poll succeeds."""
    checkpoint = ImapCheckpoint(temp_checkpoint_db)
    checkpoint.save_checkpoint('INBOX', uidvalidity=12345, last_uid=100)
    
    mock_conn = MagicMock()
    MockIMAP.return_value = mock_conn
    
    mock_conn.login.return_value = ('OK', [])
    mock_conn.select.return_value = ('OK', [])
    mock_conn.status.return_value = ('OK', [b'INBOX (UIDVALIDITY 12345)'])
    
    # First poll: UID 101 fails with exception
    mock_conn.uid.side_effect = [
        ('OK', [b'101']),
        ('OK', [(b'101 (RFC822 {123}', create_mock_email(message_id='msg101'))])
    ]
    mock_conn.logout.return_value = ('OK', [])
    
    poller = GmailImapPoller(
        host='imap.gmail.com',
        port=993,
        username='test@gmail.com',
        password='app-password',
        folder='INBOX',
        checkpoint_db=temp_checkpoint_db,
        use_ssl=True
    )
    
    mock_conversation_manager = Mock()
    mock_conversation_manager.receive_email.side_effect = Exception("Transient error")
    
    stats1 = poller.poll(mock_conversation_manager, {}, {})
    
    assert stats1['failed_transient'] == 1
    assert checkpoint.get_checkpoint('INBOX')[1] == 100  # Not advanced
    
    # Second poll: Retry UID 101, now succeeds
    mock_conn.uid.side_effect = [
        ('OK', [b'101']),
        ('OK', [(b'101 (RFC822 {123}', create_mock_email(message_id='msg101'))])
    ]
    mock_conversation_manager.receive_email.side_effect = None
    mock_conversation_manager.receive_email.return_value = Mock(success=True, reason="Processed")
    
    stats2 = poller.poll(mock_conversation_manager, {}, {})
    
    assert stats2['processed'] == 1
    assert stats2['failed_transient'] == 0
    assert checkpoint.get_checkpoint('INBOX')[1] == 101  # Advanced after success


@patch('agent.imap_poller.imaplib.IMAP4_SSL')
def test_poller_quarantine_failure_stops_processing(MockIMAP, temp_checkpoint_db):
    """If quarantine fails, checkpoint does NOT advance and processing stops."""
    checkpoint = ImapCheckpoint(temp_checkpoint_db)
    checkpoint.save_checkpoint('INBOX', uidvalidity=12345, last_uid=100)
    
    mock_conn = MagicMock()
    MockIMAP.return_value = mock_conn
    
    mock_conn.login.return_value = ('OK', [])
    mock_conn.select.return_value = ('OK', [])
    mock_conn.status.return_value = ('OK', [b'INBOX (UIDVALIDITY 12345)'])
    
    # Two messages: 101 and 102
    mock_conn.uid.side_effect = [
        ('OK', [b'101 102']),
        ('OK', [(b'101 (RFC822 {123}', create_mock_email())]),
        ('OK', [(b'102 (RFC822 {123}', create_mock_email())])
    ]
    mock_conn.logout.return_value = ('OK', [])
    
    poller = GmailImapPoller(
        host='imap.gmail.com',
        port=993,
        username='test@gmail.com',
        password='app-password',
        folder='INBOX',
        checkpoint_db=temp_checkpoint_db,
        use_ssl=True
    )
    
    # First message fails, and quarantine also fails (simulated by corrupting DB)
    mock_conversation_manager = Mock()
    mock_conversation_manager.receive_email.return_value = Mock(success=False, reason="Failed")
    
    # Corrupt the checkpoint database to make quarantine fail
    with sqlite3.connect(temp_checkpoint_db) as conn:
        conn.execute("DROP TABLE imap_quarantine")
        conn.commit()
    
    stats = poller.poll(mock_conversation_manager, {}, {})
    
    assert stats['fetched'] == 2
    assert stats['processed'] == 0
    assert stats['quarantined'] == 0
    assert stats['failed_transient'] == 1  # Quarantine failed
    
    # Checkpoint should NOT advance (still at 100)
    result = checkpoint.get_checkpoint('INBOX')
    assert result[0] == 12345
    assert result[1] == 100
    
    # Second message (102) should not be processed (stopped at first failure)
    assert mock_conversation_manager.receive_email.call_count == 1


@patch('agent.imap_poller.imaplib.IMAP4_SSL')
def test_poller_later_messages_after_failed_uid(MockIMAP, temp_checkpoint_db):
    """After restart, poller retries the failed UID before processing later ones."""
    checkpoint = ImapCheckpoint(temp_checkpoint_db)
    # Checkpoint stuck at 100 (UID 101 failed previously)
    checkpoint.save_checkpoint('INBOX', uidvalidity=12345, last_uid=100)
    
    mock_conn = MagicMock()
    MockIMAP.return_value = mock_conn
    
    mock_conn.login.return_value = ('OK', [])
    mock_conn.select.return_value = ('OK', [])
    mock_conn.status.return_value = ('OK', [b'INBOX (UIDVALIDITY 12345)'])
    
    # Messages 101, 102, 103 available
    mock_conn.uid.side_effect = [
        ('OK', [b'101 102 103']),
        ('OK', [(b'101 (RFC822 {123}', create_mock_email(message_id='msg101'))]),
        ('OK', [(b'102 (RFC822 {123}', create_mock_email(message_id='msg102'))]),
        ('OK', [(b'103 (RFC822 {123}', create_mock_email(message_id='msg103'))])
    ]
    mock_conn.logout.return_value = ('OK', [])
    
    poller = GmailImapPoller(
        host='imap.gmail.com',
        port=993,
        username='test@gmail.com',
        password='app-password',
        folder='INBOX',
        checkpoint_db=temp_checkpoint_db,
        use_ssl=True
    )
    
    # All messages succeed this time
    mock_conversation_manager = Mock()
    mock_conversation_manager.receive_email.return_value = Mock(success=True)
    
    stats = poller.poll(mock_conversation_manager, {}, {})
    
    assert stats['fetched'] == 3
    assert stats['processed'] == 3
    
    # All three messages should be processed in order
    calls = mock_conversation_manager.receive_email.call_args_list
    assert calls[0][1]['message_id'] == 'msg101'
    assert calls[1][1]['message_id'] == 'msg102'
    assert calls[2][1]['message_id'] == 'msg103'
    
    # Checkpoint should advance to 103
    result = checkpoint.get_checkpoint('INBOX')
    assert result[0] == 12345
    assert result[1] == 103


@patch('agent.imap_poller.imaplib.IMAP4_SSL')
def test_poller_parse_failure_quarantines(MockIMAP, temp_checkpoint_db):
    """Message that fails parsing is quarantined before checkpoint advances."""
    checkpoint = ImapCheckpoint(temp_checkpoint_db)
    checkpoint.save_checkpoint('INBOX', uidvalidity=12345, last_uid=100)
    
    mock_conn = MagicMock()
    MockIMAP.return_value = mock_conn
    
    mock_conn.login.return_value = ('OK', [])
    mock_conn.select.return_value = ('OK', [])
    mock_conn.status.return_value = ('OK', [b'INBOX (UIDVALIDITY 12345)'])
    
    # Corrupt message that will fail parsing
    corrupt_msg = b'\x80\x81\x82 invalid email'
    
    mock_conn.uid.side_effect = [
        ('OK', [b'101']),
        ('OK', [(b'101 (RFC822 {123}', corrupt_msg)])
    ]
    mock_conn.logout.return_value = ('OK', [])
    
    poller = GmailImapPoller(
        host='imap.gmail.com',
        port=993,
        username='test@gmail.com',
        password='app-password',
        folder='INBOX',
        checkpoint_db=temp_checkpoint_db,
        use_ssl=True
    )
    
    mock_conversation_manager = Mock()
    stats = poller.poll(mock_conversation_manager, {}, {})
    
    assert stats['fetched'] == 1
    assert stats['quarantined'] == 1
    assert stats['processed'] == 0
    
    # Checkpoint should advance after quarantine
    result = checkpoint.get_checkpoint('INBOX')
    assert result[0] == 12345
    assert result[1] == 101
    
    # Verify quarantine contains the corrupt message
    with sqlite3.connect(temp_checkpoint_db) as conn:
        cursor = conn.execute(
            "SELECT raw_message, failure_reason FROM imap_quarantine WHERE uid = 101"
        )
        row = cursor.fetchone()
        assert row[0] == corrupt_msg
        assert row[1] == 'parse_error'


@patch('agent.imap_poller.imaplib.IMAP4_SSL')
def test_poller_auto_reply_skips_and_advances(MockIMAP, temp_checkpoint_db):
    """Auto-reply messages are skipped but checkpoint advances."""
    checkpoint = ImapCheckpoint(temp_checkpoint_db)
    checkpoint.save_checkpoint('INBOX', uidvalidity=12345, last_uid=100)
    
    mock_conn = MagicMock()
    MockIMAP.return_value = mock_conn
    
    mock_conn.login.return_value = ('OK', [])
    mock_conn.select.return_value = ('OK', [])
    mock_conn.status.return_value = ('OK', [b'INBOX (UIDVALIDITY 12345)'])
    
    auto_reply_msg = create_mock_email(auto_reply=True)
    
    mock_conn.uid.side_effect = [
        ('OK', [b'101']),
        ('OK', [(b'101 (RFC822 {123}', auto_reply_msg)])
    ]
    mock_conn.logout.return_value = ('OK', [])
    
    poller = GmailImapPoller(
        host='imap.gmail.com',
        port=993,
        username='test@gmail.com',
        password='app-password',
        folder='INBOX',
        checkpoint_db=temp_checkpoint_db,
        use_ssl=True
    )
    
    mock_conversation_manager = Mock()
    stats = poller.poll(mock_conversation_manager, {}, {})
    
    assert stats['fetched'] == 1
    assert stats['skipped'] == 1
    assert stats['processed'] == 0
    
    # Checkpoint should advance
    result = checkpoint.get_checkpoint('INBOX')
    assert result[0] == 12345
    assert result[1] == 101
    
    # Conversation manager should NOT be called
    mock_conversation_manager.receive_email.assert_not_called()


@patch('agent.imap_poller.imaplib.IMAP4_SSL')
def test_poller_preserves_threading_headers(MockIMAP, temp_checkpoint_db):
    """Poller preserves In-Reply-To and References headers."""
    checkpoint = ImapCheckpoint(temp_checkpoint_db)
    checkpoint.save_checkpoint('INBOX', uidvalidity=12345, last_uid=100)
    
    mock_conn = MagicMock()
    MockIMAP.return_value = mock_conn
    
    mock_conn.login.return_value = ('OK', [])
    mock_conn.select.return_value = ('OK', [])
    mock_conn.status.return_value = ('OK', [b'INBOX (UIDVALIDITY 12345)'])
    
    reply_msg = create_mock_email(
        subject="Re: Appointment",
        message_id="reply123@example.com",
        in_reply_to="original456@example.com"
    )
    
    mock_conn.uid.side_effect = [
        ('OK', [b'101']),
        ('OK', [(b'101 (RFC822 {123}', reply_msg)])
    ]
    mock_conn.logout.return_value = ('OK', [])
    
    poller = GmailImapPoller(
        host='imap.gmail.com',
        port=993,
        username='test@gmail.com',
        password='app-password',
        folder='INBOX',
        checkpoint_db=temp_checkpoint_db,
        use_ssl=True
    )
    
    mock_conversation_manager = Mock()
    mock_conversation_manager.receive_email.return_value = Mock(success=True)
    
    stats = poller.poll(mock_conversation_manager, {}, {})
    
    # Verify threading headers were passed
    call_args = mock_conversation_manager.receive_email.call_args
    assert call_args[1]['message_id'] == 'reply123@example.com'
    assert call_args[1]['headers']['In-Reply-To'] == '<original456@example.com>'
    assert 'References' in call_args[1]['headers']
