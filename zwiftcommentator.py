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
    
    Efter hvert navn du nævner skal du skrive (ZwiftID: <ZwiftID>)

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
    Du er Jørgen Leth – cykelkommentator, poet og filmskaber.

Opgave  
Skriv en kort, eftertænksom, let ironisk og poetisk kommentar til Discord om dagens Zwift‑opgraderinger i Danish Zwift Racers (DZR).

Datafelter  
- “upgradedZPCategory”: ryttere der har forbedret deres Zwift Pace Group  
- “upgradedZwiftRacingCategory”: ryttere der har forbedret deres Zwift Racing vELO‑kategori  

Stil  
- Rolig, observerende, sanselig; brug rytmiske, filmiske billeder  
- Korte sætninger. Små pauser. Et blik ind i rytterens bevægelse.  
- Underspillet begejstring. Subtil humor.  
- Brug em‑dashes til refleksion (“—”), og indskud som “jeg ser det for mig”.  
- Få, velvalgte emojis (🚴‍♂️✨) – højst 2‑3 i alt.  
- Efter hvert navn: “(ZwiftID: <ZwiftID>)” i parentes.  
- Afslut med en stille punchline i Leth‑stil, fx: “DZR — fordi vi altid leder efter den næste lille bevægelse fremad.”

    Data:

    {json.dumps(data, ensure_ascii=False, indent=2)}

    Kommentar:
    """

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "Du er Jørgen Leth. Du kommenterer DZR‑opgraderinger med hans rolige, poetiske "
                    "fortællestemme og underspillede humor."},
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
            
        Returns:
            str: Modified message with Discord mentions
        """

        # Get Discord users from Firebase
        discord_users = fb.get_collection("discord_users")
         
        # Create a lookup dictionary of ZwiftIDs to Discord IDs
        zwiftid_to_discord = {}
        for user in discord_users:
            if "zwiftID" in user and "discordID" in user:
                # Store the mapping from ZwiftID to Discord ID
                zwiftid_to_discord[user["zwiftID"]] = user["discordID"]
        
        # Replace names with Discord mentions in the message
        modified_message = message
        
        # Replace (ZwiftID: <ZwiftID>) with Discord mentions in the message
        for zwiftid,discord_id in zwiftid_to_discord.items():
            
            pattern = '(ZwiftID: ' + zwiftid + ')'
            
            # Replace with Discord mention format: <@DISCORD_ID>
            modified_message = re.sub(pattern, f"<@{discord_id}>", modified_message, flags=re.IGNORECASE)
        
        # Remove any remaining (ZwiftID: X) patterns that weren't replaced
        modified_message = re.sub(r'\(ZwiftID: [^)]+\)', '', modified_message)
        
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

