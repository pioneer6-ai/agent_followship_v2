#!/usr/bin/env python3
"""
Staff account setup utility.

Creates staff accounts with securely hashed passwords.
Run this script to set up initial staff credentials.

Usage:
    python scripts/setup_staff_accounts.py

Security:
- Passwords are never stored in plaintext
- Password requirements: minimum 8 characters
- Session secret generated securely
- Accounts stored in auth.db separate from scheduling.db
"""

import sys
import getpass
from pathlib import Path

# Allow running from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web.auth import AuthDatabase


def setup_staff_account():
    """Interactive staff account creation."""
    print("\n" + "="*60)
    print("Staff Account Setup")
    print("="*60 + "\n")
    
    auth_db = AuthDatabase()
    
    # Get username
    while True:
        username = input("Enter username: ").strip()
        if username:
            break
        print("Username cannot be empty.\n")
    
    # Get password
    while True:
        password = getpass.getpass("Enter password (min 8 characters): ")
        if len(password) < 8:
            print("Password must be at least 8 characters.\n")
            continue
        
        password_confirm = getpass.getpass("Confirm password: ")
        if password != password_confirm:
            print("Passwords do not match.\n")
            continue
        
        break
    
    # Get role
    while True:
        print("\nAvailable roles:")
        print("  1. staff  - Can approve/decline appointments")
        print("  2. admin  - Full administrative access")
        role_choice = input("Select role (1 or 2): ").strip()
        
        if role_choice == '1':
            role = 'staff'
            break
        elif role_choice == '2':
            role = 'admin'
            break
        else:
            print("Invalid choice. Please enter 1 or 2.\n")
    
    # Create account
    print(f"\nCreating account for '{username}' with role '{role}'...")
    success, error_msg = auth_db.create_staff_account(username, password, role)
    
    if success:
        print(f"✓ Account created successfully!")
        print(f"\nUsername: {username}")
        print(f"Role: {role}")
        print("\nThe user can now log in via the web interface.")
    else:
        print(f"✗ Error: {error_msg}")
        return False
    
    return True


def list_staff_accounts():
    """List existing staff accounts."""
    auth_db = AuthDatabase()
    
    with auth_db._get_connection() as conn:
        rows = conn.execute('''
            SELECT username, role, created_at, last_login, is_active
            FROM staff_accounts
            ORDER BY created_at
        ''').fetchall()
    
    if not rows:
        print("\nNo staff accounts found.")
        return
    
    print("\n" + "="*60)
    print("Existing Staff Accounts")
    print("="*60)
    
    for row in rows:
        status = "Active" if row['is_active'] else "Inactive"
        last_login = row['last_login'] or "Never"
        print(f"\nUsername: {row['username']}")
        print(f"  Role: {row['role']}")
        print(f"  Status: {status}")
        print(f"  Created: {row['created_at'][:10]}")
        print(f"  Last Login: {last_login[:10] if last_login != 'Never' else last_login}")


def main():
    """Main menu."""
    while True:
        print("\n" + "="*60)
        print("Staff Account Management")
        print("="*60)
        print("\n1. Create new staff account")
        print("2. List existing accounts")
        print("3. Exit")
        
        choice = input("\nSelect option: ").strip()
        
        if choice == '1':
            setup_staff_account()
        elif choice == '2':
            list_staff_accounts()
        elif choice == '3':
            print("\nExiting.")
            break
        else:
            print("\nInvalid choice.")


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nInterrupted.")
        sys.exit(0)
