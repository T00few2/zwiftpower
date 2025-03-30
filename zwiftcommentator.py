import openai
import json
import requests
from openai import OpenAI

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

    def send_to_discord_api(self, channel_id: str, message: str, api_url: str):
        payload = {
            "channelId": channel_id,
            "messageContent": message
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

