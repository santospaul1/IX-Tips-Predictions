# ⚽ IX-Tips – Football Match Prediction System

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

- **Backend:** Django, Celery, Redis, PostgreSQL/SQLite
- **Machine Learning:** scikit-learn (RandomForest, LabelEncoder)
- **Frontend:** Bootstrap, jQuery, AJAX
- **Data Source:** [football-data.org](https://football-data.org)

---

## 🚀 Setup Instructions

### 1. Clone the project

```bash
git clone https://github.com/IX-Tips/IX-Tips-predictions.git
cd IX-Tips-predictions
