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

