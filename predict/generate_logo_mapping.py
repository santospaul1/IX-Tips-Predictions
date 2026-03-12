import os

from django.conf import settings

# Folder where logos are stored, relative to the static folder
LOGO_DIR = os.path.join(settings.BASE_DIR, "static", "logos")

TEAM_LOGOS = {}

if os.path.isdir(LOGO_DIR):
    for filename in os.listdir(LOGO_DIR):
        if filename.endswith(".png"):
            # Normalize filename to team name (e.g., manchester_united.png → Manchester United)
            team_name = filename[:-4].replace("_", " ").title()
            TEAM_LOGOS[team_name] = f"logos/{filename}"
