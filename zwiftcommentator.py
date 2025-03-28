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
Du er en dansk sports-kommentator der dækker Zwift-løb for klubben DZR.

Her er data fra den seneste uge med løbsresultater og topplaceringer. 
Skriv en kort, spændende kommentar til Discord-kanalen med overblik over, 
hvilke ryttere der har klaret sig godt, hvilke løb der havde flest deltagere, 
og nævn specifikt nogle ryttere med topplaceringer. 
Brug en engagerende og lidt dramatisk stil.

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
            temperature=0.8,
            max_tokens=600
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

