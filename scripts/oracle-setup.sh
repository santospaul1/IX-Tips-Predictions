#!/bin/bash
# ── IX-Tips Oracle Cloud Free Tier Setup ─────────────────────────────────
# Run on a fresh Oracle Linux 8 / Ubuntu 22.04 ARM Ampere instance.
# Creates: Docker, PostgreSQL, gunicorn app, supercronic, nginx, SSL.
set -e

APP_DIR="/opt/ix-tips"
DB_PASSWORD=$(openssl rand -base64 24 | tr -d '/+=' | head -20)
SECRET_KEY=$(openssl rand -base64 48 | tr -d '/+=' | head -50)
DOMAIN="${1:-ix-tips-predictions.fly.dev}"  # replace with your domain

echo "=== Installing system packages ==="
if command -v dnf &>/dev/null; then
    # Oracle Linux / RHEL
    sudo dnf update -y
    sudo dnf install -y docker docker-compose nginx certbot python3-certbot-nginx git curl
elif command -v apt &>/dev/null; then
    # Ubuntu
    sudo apt update && sudo apt upgrade -y
    sudo apt install -y docker.io docker-compose nginx certbot python3-certbot-nginx git curl
fi

echo "=== Starting Docker ==="
sudo systemctl enable --now docker
sudo usermod -aG docker $USER

echo "=== Setting up PostgreSQL ==="
sudo docker run -d --name ix-tips-db --restart always \
    -e POSTGRES_PASSWORD="$DB_PASSWORD" \
    -e POSTGRES_DB=ix_tips \
    -p 127.0.0.1:5432:5432 \
    -v pgdata:/var/lib/postgresql/data \
    postgres:16-alpine

sleep 3
echo "DB_PASSWORD=$DB_PASSWORD" > "$APP_DIR/.env"

echo "=== Cloning IX-Tips ==="
sudo mkdir -p "$APP_DIR"
sudo chown $USER:$USER "$APP_DIR"
git clone https://github.com/santospaul1/IX-Tips-Predictions.git "$APP_DIR"
cd "$APP_DIR"

echo "=== Writing .env ==="
cat > .env << EOF
DEBUG=False
SECRET_KEY=$SECRET_KEY
DATABASE_URL=postgres://postgres:${DB_PASSWORD}@localhost:5432/ix_tips
ALLOWED_HOSTS=$DOMAIN,localhost,127.0.0.1
CORS_ALLOWED_ORIGINS=https://$DOMAIN
FOOTBALL_DATA_API_KEY=${FOOTBALL_DATA_API_KEY:-}
ODDS_API_KEY=${ODDS_API_KEY:-}
APIFOOTBALL_KEY=${APIFOOTBALL_KEY:-}
LIVEFOOTBALL_KEY=${LIVEFOOTBALL_KEY:-}
EOF

echo "=== Setting Fly secrets as env vars ==="
# Copy secrets from Fly — run this on your local machine first:
#   fly secrets list --app ix-tips-predictions
# Then paste values here or set them before running this script.

echo "=== Building and starting Docker containers ==="
cat > docker-compose.yml << 'DOCKEREOF'
version: "3.8"
services:
  app:
    build: .
    restart: always
    ports:
      - "127.0.0.1:8000:8080"
    env_file:
      - .env
    depends_on:
      - db
    volumes:
      - model_cache:/tmp/ix_tips_model_cache
      - backups:/code/backups

  cron:
    build: .
    restart: always
    command: supercronic /code/crontab
    env_file:
      - .env
    depends_on:
      - db
      - app
    volumes:
      - model_cache:/tmp/ix_tips_model_cache
      - backups:/code/backups

  db:
    image: postgres:16-alpine
    restart: always
    env_file:
      - .env
    environment:
      POSTGRES_PASSWORD: "${DB_PASSWORD}"
      POSTGRES_DB: ix_tips
    ports:
      - "127.0.0.1:5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data

volumes:
  pgdata:
  model_cache:
  backups:
DOCKEREOF

sudo docker-compose up -d --build

echo "=== Running migrations ==="
sudo docker-compose exec app python manage.py migrate --noinput
sudo docker-compose exec app python manage.py collectstatic --noinput
sudo docker-compose exec app python manage.py createcachetable

echo "=== Setting up nginx ==="
cat > /tmp/ix-tips-nginx << NGINXEOF
server {
    listen 80;
    server_name $DOMAIN;

    location /static/ {
        alias $APP_DIR/staticfiles/;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 120s;
    }
}
NGINXEOF

sudo mv /tmp/ix-tips-nginx /etc/nginx/sites-available/ix-tips
sudo ln -sf /etc/nginx/sites-available/ix-tips /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl restart nginx

echo "=== Setting up SSL ==="
sudo certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m admin@$DOMAIN || true

echo "=== Setting up daily backups ==="
cat > /tmp/ix-tips-backup << 'BACKUPEOF'
#!/bin/bash
BACKUP_DIR=/opt/ix-tips/backups
mkdir -p "$BACKUP_DIR"
TIMESTAMP=$(date +%Y%m%d_%H%M)
docker exec ix-tips-db pg_dump -U postgres -Fc ix_tips > "$BACKUP_DIR/ix_tips_$TIMESTAMP.dump"
find "$BACKUP_DIR" -name "*.dump" -mtime +7 -delete
BACKUPEOF
sudo mv /tmp/ix-tips-backup /etc/cron.daily/ix-tips-backup
sudo chmod +x /etc/cron.daily/ix-tips-backup

echo "=== Done ==="
echo "App: https://$DOMAIN"
echo "Health: https://$DOMAIN/health/"
echo "Admin: https://$DOMAIN/admin-dashboard/"
echo "Next: python manage.py run_task predictions (for initial training)"
