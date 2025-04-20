import firebase_admin
from firebase_admin import firestore
from typing import List, Dict, Any, Optional
from datetime import datetime

# No credentials needed - uses Application Default Credentials
app = firebase_admin.initialize_app()
db = firestore.client()

def get_document(collection: str, doc_id: str) -> Optional[Dict[str, Any]]:
    """
    Fetch a single document from Firestore by collection and document ID.
    
    Args:
        collection: The collection name
        doc_id: The document ID
        
    Returns:
        Document data as dictionary or None if not found
    """
    doc_ref = db.collection(collection).document(doc_id)
    doc = doc_ref.get()
    
    if doc.exists:
        return doc.to_dict()
    return None

def get_collection(collection: str, limit: int = 100) -> List[Dict[str, Any]]:
    """
    Fetch all documents from a collection with optional limit.
    
    Args:
        collection: The collection name
        limit: Maximum number of documents to retrieve (default: 100)
        
    Returns:
        List of document data as dictionaries
    """
    docs = db.collection(collection).limit(limit).stream()
    return [doc.to_dict() for doc in docs]

def get_latest_document(collection: str) -> Optional[Dict[str, Any]]:
        """
        Fetch the latest document from a collection.
        
        Args:
            collection: The collection name
            
        Returns:
            The latest document as a dictionary, or None if no documents exist
        """
        docs = db.collection(collection).order_by('timestamp', direction=firestore.Query.DESCENDING).limit(1).stream()
        return [doc.to_dict() for doc in docs]  

def query_collection(
    collection: str, 
    field: str, 
    operator: str, 
    value: Any,
    limit: int = 100
) -> List[Dict[str, Any]]:
    """
    Query documents in a collection based on field, operator, and value.
    
    Args:
        collection: The collection name
        field: The field to query on
        operator: The operator to use ('==', '>', '<', '>=', '<=', 'array_contains')
        value: The value to compare against
        limit: Maximum number of documents to retrieve (default: 100)
        
    Returns:
        List of document data as dictionaries
    """
    docs = db.collection(collection).where(field, operator, value).limit(limit).stream()
    return [doc.to_dict() for doc in docs]

def get_documents_by_field(
    collection: str, 
    field: str, 
    value: Any,
    limit: int = 100
) -> List[Dict[str, Any]]:
    """
    Get documents where a field equals a specific value.
    
    Args:
        collection: The collection name
        field: The field to match
        value: The value to match
        limit: Maximum number of documents to retrieve (default: 100)
        
    Returns:
        List of document data as dictionaries
    """
    return query_collection(collection, field, "==", value, limit)

def update_discord_zwift_link(discord_id: str, zwift_id: str, username: str = None) -> Dict[str, Any]:
    """
    Update or create a Discord user with a ZwiftID.
    
    Args:
        discord_id: The Discord user ID
        zwift_id: The Zwift rider ID to link
        username: The Discord username
        
    Returns:
        Dict with operation status
    """
    # Check if user exists
    existing_docs = query_collection("discord_users", "discordID", "==", discord_id, limit=1)
    
    # Prepare data
    now = datetime.now()
    data = {
        "discordID": discord_id,
        "zwiftID": zwift_id,
        "linkedAt": now
    }
    
    # Add username if provided
    if username:
        data["username"] = username
    
    # Use the discord_id as the document ID
    doc_ref = db.collection("discord_users").document(discord_id)
    
    if existing_docs:
        # Update existing document
        doc_ref.update(data)
        return {"status": "updated", "discord_id": discord_id, "zwift_id": zwift_id}
    else:
        # Create new document with discord_id as document ID
        doc_ref.set(data)
        return {"status": "created", "discord_id": discord_id, "zwift_id": zwift_id} 