"""
Migration Script: Merge discord_users + user_profiles → users collection

This script:
1. Reads all documents from discord_users
2. Reads all documents from user_profiles  
3. Merges them into a NEW 'users' collection
4. PRESERVES the original collections (no deletion)

Run with: python migrate_users.py [--dry-run]

Use --dry-run first to see what would be migrated without making changes.
"""

import sys
import os
from datetime import datetime
import firebase_admin
from firebase_admin import credentials, firestore

# Initialize Firebase with service account
script_dir = os.path.dirname(os.path.abspath(__file__))
service_account_path = os.path.join(script_dir, 'service-account-key.json')

if not os.path.exists(service_account_path):
    print(f"ERROR: Service account key not found at: {service_account_path}")
    print("Please place your service-account-key.json file in the zwiftpower folder.")
    sys.exit(1)

cred = credentials.Certificate(service_account_path)
try:
    app = firebase_admin.get_app()
except ValueError:
    app = firebase_admin.initialize_app(cred)

db = firestore.client()

def get_all_documents(collection_name: str) -> dict:
    """Fetch all documents from a collection, keyed by document ID."""
    docs = {}
    try:
        collection_ref = db.collection(collection_name)
        for doc in collection_ref.stream():
            docs[doc.id] = doc.to_dict()
        print(f"  ✓ Read {len(docs)} documents from '{collection_name}'")
    except Exception as e:
        print(f"  ✗ Error reading '{collection_name}': {e}")
    return docs


def merge_user_data(discord_users: dict, user_profiles: dict) -> dict:
    """
    Merge discord_users and user_profiles into unified users structure.
    
    discord_users (keyed by discordId):
      - discordID: string
      - username: string
      - zwiftID: string
      - linkedAt: timestamp
    
    user_profiles (keyed by Firebase UID):
      - discordId: string
      - email: string
      - updatedAt: timestamp
    
    Result users (keyed by discordId):
      - discordId: string (normalized)
      - username: string
      - email: string | null
      - zwiftId: string | null (normalized from zwiftID)
      - zwiftLinkedAt: timestamp | null
      - firebaseUid: string | null
      - createdAt: timestamp
      - updatedAt: timestamp
    """
    merged = {}
    
    # First, add all discord_users
    for doc_id, data in discord_users.items():
        discord_id = str(data.get('discordID') or doc_id).strip()
        if not discord_id:
            continue
            
        merged[discord_id] = {
            'discordId': discord_id,
            'username': data.get('username') or None,
            'email': None,  # Will be filled from user_profiles if available
            'zwiftId': str(data.get('zwiftID')) if data.get('zwiftID') else None,
            'zwiftLinkedAt': data.get('linkedAt') or None,
            'firebaseUid': None,  # Will be filled from user_profiles if available
            'createdAt': data.get('linkedAt') or datetime.utcnow(),
            'updatedAt': datetime.utcnow(),
            '_source': ['discord_users']
        }
    
    # Then, merge in user_profiles data
    for firebase_uid, data in user_profiles.items():
        discord_id = str(data.get('discordId') or '').strip()
        if not discord_id:
            continue
        
        if discord_id in merged:
            # Merge into existing record
            merged[discord_id]['email'] = data.get('email') or merged[discord_id].get('email')
            merged[discord_id]['firebaseUid'] = firebase_uid
            merged[discord_id]['_source'].append('user_profiles')
            # Update timestamp if user_profiles has a newer one
            up_updated = data.get('updatedAt')
            if up_updated:
                merged[discord_id]['updatedAt'] = up_updated
        else:
            # Create new record from user_profiles only
            merged[discord_id] = {
                'discordId': discord_id,
                'username': None,
                'email': data.get('email') or None,
                'zwiftId': None,
                'zwiftLinkedAt': None,
                'firebaseUid': firebase_uid,
                'createdAt': data.get('updatedAt') or datetime.utcnow(),
                'updatedAt': data.get('updatedAt') or datetime.utcnow(),
                '_source': ['user_profiles']
            }
    
    return merged


def write_users(merged: dict, dry_run: bool = True) -> dict:
    """Write merged users to the 'users' collection."""
    stats = {'created': 0, 'updated': 0, 'skipped': 0, 'errors': 0}
    
    for discord_id, user_data in merged.items():
        try:
            # Remove internal tracking field before writing
            data_to_write = {k: v for k, v in user_data.items() if not k.startswith('_')}
            
            if dry_run:
                sources = user_data.get('_source', [])
                print(f"    [DRY-RUN] Would write user {discord_id} (sources: {', '.join(sources)})")
                stats['created'] += 1
            else:
                # Check if document already exists
                doc_ref = db.collection('users').document(discord_id)
                existing = doc_ref.get()
                
                if existing.exists:
                    # Merge with existing data (don't overwrite)
                    doc_ref.set(data_to_write, merge=True)
                    stats['updated'] += 1
                else:
                    doc_ref.set(data_to_write)
                    stats['created'] += 1
                    
        except Exception as e:
            print(f"    ✗ Error writing user {discord_id}: {e}")
            stats['errors'] += 1
    
    return stats


def main():
    dry_run = '--dry-run' in sys.argv or '-n' in sys.argv
    
    print("=" * 60)
    print("User Collection Migration Script")
    print("discord_users + user_profiles → users")
    print("=" * 60)
    
    if dry_run:
        print("\n⚠️  DRY RUN MODE - No changes will be made\n")
    else:
        print("\n🔴 LIVE MODE - Changes will be written to Firestore\n")
        confirm = input("Type 'yes' to proceed: ")
        if confirm.lower() != 'yes':
            print("Aborted.")
            return
    
    # Step 1: Read source collections
    print("\n📖 Reading source collections...")
    discord_users = get_all_documents('discord_users')
    user_profiles = get_all_documents('user_profiles')
    
    if not discord_users and not user_profiles:
        print("\n⚠️  No data found in source collections. Nothing to migrate.")
        return
    
    # Step 2: Merge data
    print("\n🔀 Merging user data...")
    merged = merge_user_data(discord_users, user_profiles)
    print(f"  ✓ Merged into {len(merged)} unique users")
    
    # Show source breakdown
    only_discord = sum(1 for u in merged.values() if u.get('_source') == ['discord_users'])
    only_profile = sum(1 for u in merged.values() if u.get('_source') == ['user_profiles'])
    both = sum(1 for u in merged.values() if len(u.get('_source', [])) == 2)
    print(f"    - From discord_users only: {only_discord}")
    print(f"    - From user_profiles only: {only_profile}")
    print(f"    - From both (merged): {both}")
    
    # Step 3: Write to users collection
    print(f"\n💾 Writing to 'users' collection...")
    stats = write_users(merged, dry_run=dry_run)
    
    print(f"\n{'📋 DRY RUN RESULTS' if dry_run else '✅ MIGRATION COMPLETE'}:")
    print(f"  - Would create: {stats['created']}" if dry_run else f"  - Created: {stats['created']}")
    print(f"  - Would update: {stats['updated']}" if dry_run else f"  - Updated: {stats['updated']}")
    print(f"  - Errors: {stats['errors']}")
    
    print("\n" + "=" * 60)
    if dry_run:
        print("Run without --dry-run to apply changes.")
    else:
        print("Original collections (discord_users, user_profiles) are PRESERVED.")
        print("You can delete them manually after verifying the migration.")
    print("=" * 60)


if __name__ == '__main__':
    main()

