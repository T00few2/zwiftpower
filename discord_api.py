import os
import requests
from typing import Dict, List, Any, Optional
import firebase

class DiscordAPI:
    """
    Class for interacting with Discord API and merging data with ZwiftIDs from Firebase.
    """
    
    def __init__(self, bot_token: str = None, guild_id: str = None):
        """
        Initialize the Discord API client.
        
        Args:
            bot_token (str, optional): Discord bot token. If not provided, 
                                      will look for DISCORD_BOT_TOKEN environment variable.
            guild_id (str, optional): Discord guild/server ID. If not provided,
                                     will look for DISCORD_GUILD_ID environment variable.
        """
        self.bot_token = bot_token or os.getenv("DISCORD_BOT_TOKEN")
        if not self.bot_token:
            raise ValueError("Discord bot token is required. Provide it as a parameter or set DISCORD_BOT_TOKEN environment variable.")
        
        self.guild_id = guild_id or os.getenv("DISCORD_GUILD_ID")
        if not self.guild_id:
            raise ValueError("Discord guild ID is required. Provide it as a parameter or set DISCORD_GUILD_ID environment variable.")
        
        self.api_base_url = "https://discord.com/api/v10"
        self.headers = {
            "Authorization": f"Bot {self.bot_token}",
            "Content-Type": "application/json"
        }
        
        # Cache for role data
        self._roles_cache = None
    
    def get_guild_roles(self) -> Dict[str, Dict[str, Any]]:
        """
        Get all roles in the guild.
        
        Returns:
            Dict[str, Dict[str, Any]]: Dictionary mapping role IDs to role data
        """
        # Return cached roles if available
        if self._roles_cache:
            return self._roles_cache
            
        try:
            response = requests.get(
                f"{self.api_base_url}/guilds/{self.guild_id}/roles",
                headers=self.headers
            )
            response.raise_for_status()
            
            roles = response.json()
            # Create a lookup dictionary of role ID to role data
            role_lookup = {}
            for role in roles:
                role_lookup[role["id"]] = {
                    "id": role["id"],
                    "name": role["name"],
                    "color": role["color"],
                    "position": role["position"],
                    "permissions": role["permissions"],
                    "managed": role["managed"],
                    "mentionable": role["mentionable"]
                }
            
            # Cache the roles
            self._roles_cache = role_lookup
            return role_lookup
            
        except requests.RequestException as e:
            print(f"Error fetching guild roles: {e}")
            return {}
    
    def get_all_members(self, limit: int = 1000, include_role_names: bool = True) -> List[Dict[str, Any]]:
        """
        Get all members from the Discord guild.
        
        Args:
            limit (int, optional): Maximum number of members to retrieve. Defaults to 1000.
            include_role_names (bool, optional): Whether to include role names along with IDs. Defaults to True.
            
        Returns:
            List[Dict[str, Any]]: List of member data including display names, usernames, and IDs
        """
        members = []
        after = None  # Used for pagination
        
        # Get role data if we need to include role names
        role_lookup = self.get_guild_roles() if include_role_names else {}
        
        # Discord returns up to 1000 members per request, so we may need multiple requests
        while True:
            try:
                url = f"{self.api_base_url}/guilds/{self.guild_id}/members?limit=1000"
                if after:
                    url += f"&after={after}"
                
                response = requests.get(url, headers=self.headers)
                response.raise_for_status()  # Raise exception for non-200 status codes
                
                batch = response.json()
                if not batch:
                    break  # No more members
                
                for member in batch:
                    # Extract relevant member info
                    user = member.get("user", {})
                    role_ids = member.get("roles", [])
                    
                    member_data = {
                        "discordID": user.get("id"),
                        "username": user.get("username"),
                        "global_name": user.get("global_name"),
                        "display_name": member.get("nick") or user.get("global_name") or user.get("username"),
                        "avatar": user.get("avatar"),
                        "joined_at": member.get("joined_at"),
                        "role_ids": role_ids
                    }
                    
                    # Include role names if requested
                    if include_role_names and role_lookup:
                        # Get role info for each role ID
                        roles_info = []
                        for role_id in role_ids:
                            if role_id in role_lookup:
                                roles_info.append({
                                    "id": role_id,
                                    "name": role_lookup[role_id]["name"],
                                    "color": role_lookup[role_id]["color"],
                                    "position": role_lookup[role_id]["position"]
                                })
                        
                        # Sort roles by position (higher position roles first)
                        roles_info.sort(key=lambda r: r["position"], reverse=True)
                        member_data["roles"] = roles_info
                    
                    members.append(member_data)
                
                # If we've reached the requested limit or fewer than 1000 members were returned, we're done
                if len(members) >= limit or len(batch) < 1000:
                    break
                
                # Set after to the ID of the last member for the next request
                after = batch[-1]["user"]["id"]
                
            except requests.RequestException as e:
                print(f"Error fetching Discord members: {e}")
                break
        
        return members[:limit]  # Ensure we don't return more than the limit
    
    def merge_with_zwift_ids(self, include_role_names: bool = True) -> List[Dict[str, Any]]:
        """
        Merge Discord member data with ZwiftIDs from Firebase.
        
        Args:
            include_role_names (bool, optional): Whether to include role names along with IDs. Defaults to True.
            
        Returns:
            List[Dict[str, Any]]: List of merged data with Discord member info and ZwiftIDs when available
        """
        # Get all Discord members
        discord_members = self.get_all_members(include_role_names=include_role_names)
        
        # Get all users from Firebase that have ZwiftIDs
        # Use a high limit to ensure we get all users (default is only 100)
        firebase_users = firebase.get_collection("users", limit=10000)
        
        # Create a lookup dictionary of discordId to zwiftId
        zwift_lookup = {}
        for user in firebase_users:
            if "discordId" in user and "zwiftId" in user:
                zwift_lookup[user["discordId"]] = user["zwiftId"]
        
        # NEW: Build a rider stats lookup from the latest club_stats
        rider_stats_lookup: Dict[str, Dict[str, Any]] = {}
        try:
            club_stats_docs = firebase.get_latest_document('club_stats')
            if club_stats_docs and len(club_stats_docs) > 0:
                stats_doc = club_stats_docs[0]
                riders = stats_doc.get('data', {}).get('riders', []) if isinstance(stats_doc, dict) else []
                for rider in riders:
                    try:
                        rider_id = rider.get('riderId')
                        if rider_id is None:
                            continue
                        rider_key = str(rider_id)
                        entry: Dict[str, Any] = {}
                        # Real rider name from club_stats
                        if 'name' in rider and isinstance(rider.get('name'), str):
                            entry['riderName'] = rider.get('name')
                        # Pace group from ZP
                        if 'zpCategory' in rider:
                            entry['zpCategory'] = rider.get('zpCategory')
                        # Racing score (ZRS) and derived category
                        score = rider.get('racingScore')
                        if isinstance(score, (int, float)):
                            entry['racingScore'] = score
                            # Compute ZRS category using helper
                            try:
                                entry['zrsCategory'] = firebase.get_zrs_category(score)
                            except Exception:
                                pass
                        # vELO (Zwift Racing mixed category and rating)
                        current = rider.get('race', {}).get('current') if isinstance(rider.get('race'), dict) else None
                        mixed = None
                        if isinstance(current, dict):
                            mixed = current.get('mixed')
                        if isinstance(mixed, dict):
                            # Score fallbacks: mixed.rating -> current.rating -> mixed.number
                            score = mixed.get('rating')
                            if not isinstance(score, (int, float)) and isinstance(current, dict):
                                score = current.get('rating')
                            if not isinstance(score, (int, float)):
                                score = mixed.get('number')
                            if isinstance(score, (int, float)):
                                entry['veloScore'] = score
                            # Category name fallbacks: mixed.category -> mixed.name
                            category_name = mixed.get('category') or mixed.get('name')
                            if isinstance(category_name, str) and category_name:
                                entry['veloCategoryName'] = category_name
                            # Letter (if available)
                            letter = mixed.get('letter')
                            if isinstance(letter, str):
                                entry['veloCategory'] = letter
                        if entry:
                            rider_stats_lookup[rider_key] = entry
                    except Exception:
                        # Skip problematic rider entries
                        continue
        except Exception as rider_stats_err:
            # Fail gracefully; stats are optional enrichments
            print(f"Error building rider stats lookup: {rider_stats_err}")
        
        # Merge the data
        merged_data = []
        for member in discord_members:
            discord_id = member.get("discordID")
            
            # Add ZwiftID if it exists in Firebase
            if discord_id and discord_id in zwift_lookup:
                member["zwiftID"] = zwift_lookup[discord_id]
                member["has_zwift_id"] = True
                # Enrich with stats when available
                stats = rider_stats_lookup.get(str(member.get("zwiftID")))
                if isinstance(stats, dict):
                    # Use update to add only known keys
                    member.update(stats)
            else:
                member["has_zwift_id"] = False
            
            merged_data.append(member)
        
        return merged_data
    
    def find_unlinked_members(self, include_role_names: bool = True) -> List[Dict[str, Any]]:
        """
        Find Discord members that don't have a linked ZwiftID.
        
        Args:
            include_role_names (bool, optional): Whether to include role names along with IDs. Defaults to True.
            
        Returns:
            List[Dict[str, Any]]: List of members without ZwiftIDs
        """
        merged_data = self.merge_with_zwift_ids(include_role_names=include_role_names)
        return [member for member in merged_data if not member["has_zwift_id"]]
    
    def find_linked_members(self, include_role_names: bool = True) -> List[Dict[str, Any]]:
        """
        Find Discord members that have a linked ZwiftID.
        
        Args:
            include_role_names (bool, optional): Whether to include role names along with IDs. Defaults to True.
            
        Returns:
            List[Dict[str, Any]]: List of members with ZwiftIDs
        """
        merged_data = self.merge_with_zwift_ids(include_role_names=include_role_names)
        return [member for member in merged_data if member["has_zwift_id"]]
    
    def get_member_by_discord_id(self, discord_id: str, include_role_names: bool = True) -> Optional[Dict[str, Any]]:
        """
        Get a specific member by Discord ID, including ZwiftID if available.
        
        Args:
            discord_id (str): Discord user ID
            include_role_names (bool, optional): Whether to include role names along with IDs. Defaults to True.
            
        Returns:
            Optional[Dict[str, Any]]: Member data or None if not found
        """
        try:
            # Get role data if we need to include role names
            role_lookup = self.get_guild_roles() if include_role_names else {}
            
            # Get the member from Discord API
            response = requests.get(
                f"{self.api_base_url}/guilds/{self.guild_id}/members/{discord_id}", 
                headers=self.headers
            )
            response.raise_for_status()
            
            member = response.json()
            user = member.get("user", {})
            role_ids = member.get("roles", [])
            
            member_data = {
                "discordID": user.get("id"),
                "username": user.get("username"),
                "global_name": user.get("global_name"),
                "display_name": member.get("nick") or user.get("global_name") or user.get("username"),
                "avatar": user.get("avatar"),
                "joined_at": member.get("joined_at"),
                "role_ids": role_ids
            }
            
            # Include role names if requested
            if include_role_names and role_lookup:
                # Get role info for each role ID
                roles_info = []
                for role_id in role_ids:
                    if role_id in role_lookup:
                        roles_info.append({
                            "id": role_id,
                            "name": role_lookup[role_id]["name"],
                            "color": role_lookup[role_id]["color"],
                            "position": role_lookup[role_id]["position"]
                        })
                
                # Sort roles by position (higher position roles first)
                roles_info.sort(key=lambda r: r["position"], reverse=True)
                member_data["roles"] = roles_info
            
            # Check if there's a ZwiftID in Firebase
            user_doc = firebase.get_document("users", discord_id)
            
            if user_doc and "zwiftId" in user_doc:
                member_data["zwiftID"] = user_doc["zwiftId"]
                member_data["has_zwift_id"] = True
            else:
                member_data["has_zwift_id"] = False
            
            return member_data
            
        except requests.RequestException as e:
            print(f"Error fetching Discord member {discord_id}: {e}")
            return None

    def _create_dm_channel(self, user_id: str) -> Optional[str]:
        """
        Create (or fetch) a DM channel with a user.

        Args:
            user_id (str): Discord user ID

        Returns:
            Optional[str]: DM channel ID or None on failure
        """
        try:
            response = requests.post(
                f"{self.api_base_url}/users/@me/channels",
                headers=self.headers,
                json={"recipient_id": user_id},
            )
            response.raise_for_status()
            data = response.json()
            return data.get("id")
        except requests.RequestException as e:
            print(f"Error creating DM channel for {user_id}: {e}")
            return None

    def send_direct_message(self, user_id: str, content: str) -> bool:
        """
        Send a direct message to a Discord user.

        Args:
            user_id (str): Discord user ID
            content (str): Message content

        Returns:
            bool: True if the message was sent successfully, False otherwise
        """
        channel_id = self._create_dm_channel(user_id)
        if not channel_id:
            return False

        try:
            response = requests.post(
                f"{self.api_base_url}/channels/{channel_id}/messages",
                headers=self.headers,
                json={"content": content},
            )
            response.raise_for_status()
            return True
        except requests.RequestException as e:
            print(f"Error sending DM to {user_id}: {e}")
            return False


# Example usage:
# discord_api = DiscordAPI()
# 
# All members with ZwiftIDs merged:
# members = discord_api.merge_with_zwift_ids()
# 
# Only members with ZwiftIDs:
# linked = discord_api.find_linked_members()
# 
# Only members without ZwiftIDs:
# unlinked = discord_api.find_unlinked_members() 