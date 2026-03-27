# SAS Dashboard - VPS Deployment Guide (Portainer)

This guide explains how to deploy the SAS Dashboard on a VPS using Portainer Stacks.

## Prerequisites
- A VPS with Docker and Portainer installed.
- Access to your project files (via Git or uploaded directly).

## Deployment Steps (Portainer Stack)

1. **Log in to Portainer**.
2. Go to **Stacks** > **Add stack**.
3. Name your stack (e.g., `sas-dashboard`).
4. **Build the Stack**:
   - If using Git: Select **Repository** and provide your Git URL.
   - If pasting code: Select **Web editor** and paste the content of `docker-compose.yml`.
5. **Configure Environment Variables**:
   In the Portainer UI, you can override the following variables in the `docker-compose.yml` or add them manually:
   - `SECRET_KEY`: Set to a random long string for security.
   - `SAS_API_IP`: Your SAS Radius server IP.
   - `WEBHOOK_URL`: Your WhatsApp/n8n reporting webhook.
   - `DB_PATH`: Set to `/app/data/payments.db` (Default in compose).
6. **Deploy**: Click **Deploy the stack**.

## Data Persistence
The configuration uses a Docker Volume named `sas_data` mapped to `/app/data`.
- The database `payments.db` will be stored in this volume.
- Even if you update the container or restart the VPS, your data will remain safe.

## Troubleshooting
- **Logs**: Check the logs in the Portainer "Containers" view for `sas-dashboard`.
- **Port**: Ensure port `5000` is open in your VPS firewall (UFW/iptables).
- **Restart**: The container is set to `always` restart if it crashes.

---
**Note**: To update the app with new code, simply pull the latest changes in Portainer (if using Git) or update the stack and re-deploy. The volume will persist your database automatically.
