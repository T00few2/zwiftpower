import openai
import json
import requests
from openai import OpenAI
import firebase as fb
import re

class ZwiftCommentator:
    def __init__(self, api_key: str, model: str = "gpt-4o"):
        self.client = OpenAI(api_key=api_key)
        self.model = model

    def generate_commentary(self, data: dict) -> str:
        prompt = f"""
    Du er en dansk sports-kommentator, der dækker Zwift-løb for klubben DZR.

    Med afsæt i data fra den seneste uge skal du skrive en kort, engagerende og dramatisk Discord-kommentar med overblik over:

        Hvilke ryttere der har klaret sig godt (placeringer og sejre)

        Hvilke løb der havde flest DZR-deltagere (efter top_10_by_zid og top_10_by_title)

        Hvilke ryttere har vundet løb (winners)

        Hvilke ryttere har leveret flest watt/kg over 1, 5 og 20 minutter med deres placering i løbet (top_watts_per_kg_20min, top_watts_per_kg_5min,top_watts_per_kg_1min)

        Hvem har været mest aktiv (most_events_riders)

        Hvem har fået flest top-3 placeringer i deres kategori (most_top_3_riders)

    Stil og format:

        Kommentaren skal være skrevet i sportskommentator-stil, som en spændende opsummering til Discord.

        Brug et dramatisk, engagerende og humoristisk sprog.

        Brug gerne emojis og korte afsnit for at gøre det læsevenligt.

        Fremhæv navne og præstationer med fede eller kursiverede formuleringer, fx: “Philip Melchiors [DZR] fløj til sejr!”

    Kommentaren må meget gerne slutte med en kort konklusion og klubopbakning som fx:
    "DZR leverer – uge efter uge. Vi ses på rullerne!"

Databeskrivelse:

top_10_by_zid: events with most participants by race id
top_10_by_title: events with most participants by event title (can be across multiple races)
most_events_riders: riders with most completed events
most_top_3_riders: riders with most top-3 finishes in their category
winners: list of winners in races
top_watts_per_kg_20min: top riders by 20-minute power
top_watts_per_kg_5min: top riders by 5-minute power
top_watts_per_kg_1min: top riders by 1-minute power

Data:

{json.dumps(data, ensure_ascii=False, indent=2)}

Kommentar:
"""

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "Du er en passioneret dansk cykelsportskommentator."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.9,
            max_tokens=1000
        )

        return response.choices[0].message.content
    
    def generate_upgrade_comment(self, data: dict) -> str:
        prompt = f"""
    Du er Donald Trump som entusiastisk kommenterer opgraderinger i den danske Zwift-klub Danish Zwift Racers (DZR).

    Du skal skrive en kort, sjov og overdreven kommentar til Discord baseret på følgende opgraderingsdata:

    - “upgradedZPCategory”: ryttere der har forbedret deres Zwift Pace Group
    - “upgradedZwiftRacingCategory”: ryttere der har forbedret deres Zwift Racing vELO kategori

    Stil:
    - Selvsikker, sjov og overdreven rosende
    - Brug Trump-udtryk som “HUGE”, “tremendous”, “winning like never before”, “people are talking about it” etc.
    - Brug emojis og formatering (fede navne og kategorier)
    - Afslut med en punchline i Trump-stil, fx “DZR – making Zwift racing great again!”

    Data:

    {json.dumps(data, ensure_ascii=False, indent=2)}

    Kommentar:
    """

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "Du er Donald Trump og kommenterer DZR Zwift-opgraderinger i hans stil."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.9,
            max_tokens=750
        )

        return response.choices[0].message.content
    
    def tag_discord_users(self, message: str) -> str:
        """
        Replace rider names in a message with Discord mentions based on their ZwiftIDs.
        
        Args:
            message (str): The original message text with rider names
            club_riders (dict): Results from zp.analyze_team_results() containing names and ZwiftIDs
            fb (FirebaseAPI, optional): Firebase API instance. Creates one if not provided.
            
        Returns:
            str: Modified message with Discord mentions
        """     
        # Get Discord users from Firebase
        discord_users = fb.get_collection("discord_users")
        
        # Get club riders from Firebase
        club_riders = fb.get_latest_document("club_stats")[0]['data']['riders']
        
        # Create a lookup dictionary of ZwiftIDs to Discord IDs
        zwiftid_to_discord = {}
        for user in discord_users:
            if "zwiftID" in user and "discordID" in user:
                # Store the mapping from ZwiftID to Discord ID
                zwiftid_to_discord[user["zwiftID"]] = user["discordID"]
        
        print("[DEBUG] ZwiftID to Discord mapping:\n", zwiftid_to_discord)
        
        # Create a mapping of names to their ZwiftIDs from team_results
        name_to_zwiftid = {}

        # Extract name and ZwiftID from team_results
        # The exact structure depends on your team_results format
        # This is an example that may need adjustment
        for rider in club_riders:
            if 'name' in rider and 'riderId' in rider:
                name_to_zwiftid[rider['name']] = str(rider['riderId'])
                
        print("[DEBUG] Name to ZwiftID mapping:\n", name_to_zwiftid)
        
        # Replace names with Discord mentions in the message
        modified_message = message
        
        # Process each name from the team results
        for name, zwiftid in name_to_zwiftid.items():
            # Check if this ZwiftID has a corresponding Discord ID
            if zwiftid in zwiftid_to_discord:
                discord_id = zwiftid_to_discord[zwiftid]
                
                # Create a pattern to find the name in the message
                # We use word boundaries to avoid partial matches
                pattern = re.escape(name)
                print("[DEBUG] Pattern:\n", pattern)
                
                # Replace with Discord mention format: <@DISCORD_ID>
                modified_message = re.sub(pattern, f"<@{discord_id}>", modified_message, flags=re.IGNORECASE)
        
        return modified_message

    def send_to_discord_api(self, channel_id: str, message: str, api_url: str):
        """
        Send a message to a Discord channel using the Discord API.
        
        Args:
            channel_id (str): The ID of the Discord channel to send the message to
            message (str): The message content to send
            api_url (str): The URL of the Discord API
            
        Returns:
            dict: The response from the Discord API
        """
        
        # Tag Discord users in the message
        tagged_message = self.tag_discord_users(message)
        
        print("[DEBUG] Tagged message:\n", tagged_message)
        
        payload = {
            "channelId": channel_id,
            "messageContent": tagged_message
        }

        headers = {
            "Content-Type": "application/json"
        }

        try:
            response = requests.post(api_url, json=payload, headers=headers)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            print("Error sending to Discord API:", e)
            print("Response:", getattr(e.response, 'text', 'No response'))
            return None

