import firebase_admin
from firebase_admin import firestore
from typing import List, Dict, Any, Optional

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