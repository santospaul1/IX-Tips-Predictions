import requests
import json

api_key = '0b1caff9695ebeb055b600f6995a52c6' # Replace with your actual key
api_host = 'v3.football.api-sports.io'

# URL for getting fixtures
fixtures_url = 'https://v3.football.api-sports.io/fixtures'

# Parameters to get upcoming English Premier League (league_id: 39) matches
# with the correct season format
fixtures_params = {
    'league': '39', 
    'season': '2023', # Correct season format
    'next': '10' 
}

headers = {
    'x-rapidapi-host': api_host,
    'x-rapidapi-key': api_key
}

try:
    fixtures_response = requests.get(fixtures_url, headers=headers, params=fixtures_params)
    fixtures_response.raise_for_status() 
    fixtures_data = fixtures_response.json()

    fixture_ids = [match['fixture']['id'] for match in fixtures_data['response']]

except requests.exceptions.RequestException as e:
    print(f"Error fetching fixtures: {e}")
    exit()

if not fixture_ids:
    print("No upcoming fixtures found.")
    exit()
else:
    print(f"Successfully retrieved {len(fixture_ids)} fixture IDs.")
    print("Fixture IDs:", fixture_ids)