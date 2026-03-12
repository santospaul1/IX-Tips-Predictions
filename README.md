# ⚽ IX-Tips – Football Prediction System

**IX-Tips** is an intelligent football match prediction system built with **Django**, **Celery**, and **scikit-learn**. It fetches real-time fixtures, trains ML models using historical data, generates predictions, and displays daily tips with accuracy evaluation.

---

## 📌 Features

- 🔮 Predict match outcomes using machine learning (RandomForest).
- ⏰ Schedule automatic weekly predictions via **Celery**.
- 🧠 Store and compare actual results vs predictions.
- 🟢 Highlight correct, 🔴 incorrect, 🔵 upcoming predictions.
- ⚙️ Admin dashboard with live prediction tools and metadata monitoring.
- 📊 League table integration and tip filtering (e.g., Over 2.5, GG, 1X2).
- 📅 Match calendar filtering with AJAX UI.

---

## 🛠️ Technologies Used

- **Backend:** Django, Celery, Redis, SQLite
- **Machine Learning:** scikit-learn (RandomForest, LabelEncoder)
- **Frontend:** Bootstrap, jQuery, AJAX
- **Data Source:** [football-data.org](https://football-data.org)

---

## 🚀 Setup Instructions

### 1. Clone the project

   ```bash
   git clone https://github.com/nyasimi23/IX-Tips
   cd IX-Tips
   ```

### 2. Install dependencies:
   ```bash
   python -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

### 3. Set up the Football Data API:
   - Obtain an API key from [Football Data](https://www.football-data.org/).
   - Replace `API_KEY` in the code with your API key.

### 4. Run database migrations:
   ```bash
   python manage.py migrate
   ```

### 5. Start the development server:
   ```bash
   python manage.py runserver
   ```

### 6. Access the application in your browser:
   ```
   http://127.0.0.1:8000/
   ```

---

## 🧵 Celery Integration
---
### 1.Start a Redis server:
```bash
redis-server
```
#### 2.Start Celery worker:
```bash
celery -A IX_Tips worker -l info
```
### 3.Start Celery beat (optional for periodic tasks):
```bash
celery -A ix_tips beat -l info
```
---
## Supported Competitions
---
- Premier League (PL)
- La Liga (PD)
- Serie A (SA)
- Bundesliga (BL1)
- Ligue 1 (FL1)
- Eredivisie (DED)
- Primeira Liga (PPL)
- Championship (ELC)
- UEFA Champions League (CL)
- FIFA World Cup (WC)

---

🗓️ Scheduled Prediction
---
You can trigger weekly predictions manually from the Django shell:
```bash

python manage.py shell
```
```bash
from predict.tasks import schedule_predictions
schedule_predictions.delay()
```
Or stagger predictions for better load balance:
```
from predict.tasks import trigger_staggered_scheduling
trigger_staggered_scheduling.delay()
```
---
## 🎯 Prediction Logic
---
-Uses fetch_matches_by_date() to get upcoming fixtures.

-Trains RandomForest models using historical data per competition.

-Stores MatchPrediction with predicted scores.

-Computes tips like 1, X, 2, GG, Over 2.5.

-Evaluates correctness using actual scores if match is FINISHED.
---

## 🧪 Generate Fake Data
---
Generate test predictions and top picks:

```bash
python manage.py generate_fake_predictions --count 100
```
You can modify the date range in the command logic.
---

## 📈 Admin Dashboard
---
Access /admin-dashboard/ to:

-View Celery task status

-Trigger live predictions

-Monitor cache freshness

-Manually run store_top_pick_for_date()

---
## 🔄 Update Match Status (TIMED / FINISHED)
---
Run this task daily via Celery or manually:
```bash
from predict.tasks import update_match_status_task
update_match_status_task.delay()
```
---
## 📦 Deployment Notes
---
Set DEBUG = False and configure ALLOWED_HOSTS in production.

Use Gunicorn + NGINX for serving Django app.

Set up supervisord or systemd for Celery workers.
---
## 📃 License
---
This project is licensed under the MIT License. See `LICENSE` for details.
This project is for educational and personal use. Not affiliated with or endorsed by football-data.org.

---
## API Reference
---
- **Football Data API**: [Documentation](https://www.football-data.org/documentation/quickstart)

---

## 🙌 Acknowledgments
- Paul Santos
- Job Nyasimi
- Austine Ndula
- [Football Data API](https://www.football-data.org/) for providing match data.
- Open-source libraries and frameworks: Django, scikit-learn, pandas.

 
