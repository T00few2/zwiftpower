import firebase_admin
from firebase_admin import firestore
from typing import List, Dict, Any, Optional
from datetime import datetime
import re
import pytz

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

def get_collection(collection: str, limit: int = 100, include_id: bool = False) -> List[Dict[str, Any]]:
    """
    Fetch all documents from a collection with optional limit.
    
    Args:
        collection: The collection name
        limit: Maximum number of documents to retrieve (default: 100)
        include_id: Whether to include document IDs in the returned data (default: False)
        
    Returns:
        List of document data as dictionaries, optionally with document IDs included
    """
    docs = db.collection(collection).limit(limit).stream()
    result = []
    for doc in docs:
        doc_data = doc.to_dict()
        if include_id:
            doc_data['id'] = doc.id  # Add the document ID only when requested
        result.append(doc_data)
    return result

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

def set_document(collection: str, doc_id: str, data: Dict[str, Any], merge: bool = False) -> bool:
    """
    Set or update a document in Firestore.
    
    Args:
        collection: The collection name
        doc_id: The document ID
        data: The data to set/update
        merge: Whether to merge with existing data (default: False)
        
    Returns:
        True if successful, False otherwise
    """
    try:
        doc_ref = db.collection(collection).document(doc_id)
        if merge:
            doc_ref.set(data, merge=True)
        else:
            doc_ref.set(data)
        return True
    except Exception as e:
        print(f"Error setting document: {e}")
        return False

def delete_document(collection: str, doc_id: str) -> bool:
    """
    Delete a document from Firestore.

    Args:
        collection: The collection name
        doc_id: The document ID

    Returns:
        True if successful, False otherwise
    """
    try:
        db.collection(collection).document(doc_id).delete()
        return True
    except Exception as e:
        print(f"Error deleting document: {e}")
        return False

def update_discord_zwift_link(discord_id: str, zwift_id: str, username: str = None) -> Dict[str, Any]:
    """
    Update or create a user with a ZwiftID link.
    
    Args:
        discord_id: The Discord user ID
        zwift_id: The Zwift rider ID to link
        username: The Discord username
        
    Returns:
        Dict with operation status
    """
    # Check if user exists
    doc_ref = db.collection("users").document(discord_id)
    existing_doc = doc_ref.get()
    
    # Prepare data
    now = datetime.now()
    data = {
        "discordId": discord_id,
        "zwiftId": zwift_id,
        "zwiftLinkedAt": now,
        "updatedAt": now
    }
    
    # Add username if provided
    if username:
        data["username"] = username
    
    if existing_doc.exists:
        # Update existing document (merge to preserve other fields)
        doc_ref.set(data, merge=True)
        return {"status": "updated", "discord_id": discord_id, "zwift_id": zwift_id}
    else:
        # Create new document
        data["createdAt"] = now
        doc_ref.set(data)
        return {"status": "created", "discord_id": discord_id, "zwift_id": zwift_id} 
    
# ZP category rankings
zp_category_rank = {
    'D': 1,
    'C': 2,
    'B': 3,
    'A': 4,
    'A+': 5,
}

# Racing score category rankings
zrs_category_rank = {
    'E': 1,  # 1-180
    'D': 2,  # 180-350
    'C': 3,  # 350-520
    'B': 4,  # 520-690
    'A': 5,  # 690-1000
}

def get_zrs_category(score: float) -> str:
    """
    Determine ZRS category based on racing score.
    
    Args:
        score: Racing score value
        
    Returns:
        Category letter (A, B, C, D, E)
    """
    if score >= 690:
        return 'A'
    elif score >= 520:
        return 'B'
    elif score >= 350:
        return 'C'
    elif score >= 180:
        return 'D'
    else:
        return 'E'

def is_zp_category(cat: Any) -> bool:
    """
    Check if a value is a valid ZP category.
    
    Args:
        cat: The value to check
        
    Returns:
        True if the value is a valid ZP category, False otherwise
    """
    return isinstance(cat, str) and cat in zp_category_rank

def format_date(input_str: str) -> str:
    """
    Convert YYMMDD to YYYY-MM-DD format.
    
    Args:
        input_str: Date string in YYMMDD format
        
    Returns:
        Formatted date string in YYYY-MM-DD format
        
    Raises:
        ValueError: If the input is not in YYMMDD format
    """
    if not re.match(r'^\d{6}$', input_str):
        raise ValueError(f"Invalid date format: {input_str}")
    
    year = f"20{input_str[0:2]}"
    month = input_str[2:4]
    day = input_str[4:6]
    return f"{year}-{month}-{day}"

def compare_rider_categories(today_raw: str, yesterday_raw: str) -> Dict[str, Any]:
    """
    Compare rider categories between two dates to find riders who upgraded.
    
    Args:
        today_raw: Today's date in YYMMDD format
        yesterday_raw: Yesterday's date in YYMMDD format
        
    Returns:
        Dict containing comparison results with upgraded riders
        
    Raises:
        ValueError: If date formats are invalid
        Exception: For other errors during processing
    """
    try:
        if not today_raw or not yesterday_raw:
            raise ValueError("Missing required parameters: today and yesterday (YYMMDD)")

        today_id = format_date(today_raw)
        yesterday_id = format_date(yesterday_raw)

        today_doc = db.collection('club_stats').document(today_id).get()
        yesterday_doc = db.collection('club_stats').document(yesterday_id).get()

        # If a daily snapshot isn't present yet (e.g. cron runs before ingestion),
        # treat it as "no upgrades" instead of failing the whole endpoint.
        if not today_doc.exists or not yesterday_doc.exists:
            missing = []
            if not today_doc.exists:
                missing.append(today_id)
            if not yesterday_doc.exists:
                missing.append(yesterday_id)
            print(f"[WARN] compare_rider_categories: missing Firestore documents: {missing}")

            paris_tz = pytz.timezone('Europe/Paris')
            timestamp = datetime.now(paris_tz).strftime('%d/%m/%Y, %H:%M:%S')
            return {
                'message': f'No comparison data available (missing snapshots: {", ".join(missing)}).',
                'timeStamp': timestamp,
                'upgradedZPCategory': [],
                'upgradedZwiftRacingCategory': [],
                'upgradedZRSCategory': []
            }

        today_data = today_doc.to_dict()
        yesterday_data = yesterday_doc.to_dict()
        
        today_riders = (today_data.get('data') or {}).get('riders', []) if today_data else []
        yesterday_riders = (yesterday_data.get('data') or {}).get('riders', []) if yesterday_data else []

        def _norm_rider_id(v: Any) -> str | None:
            if v is None:
                return None
            if isinstance(v, (int, float)):
                return str(int(v))
            if isinstance(v, str):
                return v.strip() or None
            return str(v)

        # Normalize keys so today's/yesterday's lookup always matches (Firestore data can mix str/int IDs).
        today_map: Dict[str, Dict[str, Any]] = {}
        for r in today_riders:
            if not isinstance(r, dict):
                continue
            rid = _norm_rider_id(r.get('riderId'))
            if rid:
                today_map[rid] = r

        yesterday_map: Dict[str, Dict[str, Any]] = {}
        for r in yesterday_riders:
            if not isinstance(r, dict):
                continue
            rid = _norm_rider_id(r.get('riderId'))
            if rid:
                yesterday_map[rid] = r

        upgraded_zp_category = []
        upgraded_zwift_racing_category = []
        upgraded_zrs_category = []

        for rider_id_str, today in today_map.items():
            yesterday = yesterday_map.get(rider_id_str)
            
            if not today or not yesterday:
                continue

            name = today.get('name', 'Unknown')

            # Compare ZP categories
            today_cat = today.get('zpCategory')
            yesterday_cat = yesterday.get('zpCategory')

            if (is_zp_category(today_cat) and is_zp_category(yesterday_cat) and 
                today_cat != yesterday_cat and 
                zp_category_rank[today_cat] > zp_category_rank[yesterday_cat]):
                upgraded_zp_category.append({
                    'riderId': rider_id,
                    'name': name,
                    'from': yesterday_cat,
                    'to': today_cat
                })

            # Compare Zwift racing categories
            # 'race' (or nested keys) can be None in some snapshots; use safe access.
            today_race = today.get('race') or {}
            yesterday_race = yesterday.get('race') or {}
            today_current = (today_race.get('current') or {}) if isinstance(today_race, dict) else {}
            yesterday_current = (yesterday_race.get('current') or {}) if isinstance(yesterday_race, dict) else {}
            today_mixed = (today_current.get('mixed')) if isinstance(today_current, dict) else None
            yesterday_mixed = (yesterday_current.get('mixed')) if isinstance(yesterday_current, dict) else None

            if (isinstance(today_mixed, dict) and isinstance(yesterday_mixed, dict) and
                isinstance(today_mixed.get('number'), (int, float)) and
                isinstance(yesterday_mixed.get('number'), (int, float)) and
                today_mixed.get('number') < yesterday_mixed.get('number')):
                rider_id = int(rider_id_str) if rider_id_str.isdigit() else rider_id_str
                upgraded_zwift_racing_category.append({
                    'riderId': rider_id,
                    'name': name,
                    'from': yesterday_mixed,
                    'to': today_mixed
                })
                
            # Compare Racing Scores (ZRSCategory)
            today_score = today.get('racingScore')
            yesterday_score = yesterday.get('racingScore')
            
            if (isinstance(today_score, (int, float)) and 
                isinstance(yesterday_score, (int, float))):
                today_zrs_cat = get_zrs_category(today_score)
                yesterday_zrs_cat = get_zrs_category(yesterday_score)
                
                if (today_zrs_cat != yesterday_zrs_cat and
                    zrs_category_rank[today_zrs_cat] > zrs_category_rank[yesterday_zrs_cat]):
                    rider_id = int(rider_id_str) if rider_id_str.isdigit() else rider_id_str
                    upgraded_zrs_category.append({
                        'riderId': rider_id,
                        'name': name,
                        'from': {
                            'category': yesterday_zrs_cat,
                            'score': yesterday_score
                        },
                        'to': {
                            'category': today_zrs_cat,
                            'score': today_score
                        }
                    })

        # Format timestamp in CET/CEST timezone
        paris_tz = pytz.timezone('Europe/Paris')
        timestamp = datetime.now(paris_tz).strftime('%d/%m/%Y, %H:%M:%S')

        return {
            'message': 'Comparison complete.',
            'timeStamp': timestamp,
            'upgradedZPCategory': upgraded_zp_category,
            'upgradedZwiftRacingCategory': upgraded_zwift_racing_category,
            'upgradedZRSCategory': upgraded_zrs_category
        }

    except Exception as err:
        print(f"Comparison error: {str(err)}")
        raise 