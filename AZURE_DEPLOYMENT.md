# Azure Deployment Notes

## Important constraint: Azure Functions and a persistent Telethon listener

Telethon's `run_until_disconnected()` keeps an open connection to Telegram and
reacts to events in real time. Azure Functions (Consumption plan) is built
for short-lived, triggered executions — it is not designed to keep a socket
open indefinitely, and the platform will recycle or stop the function host
between invocations. Using a Timer Trigger that runs every few minutes would
mean you only catch messages sent during the brief execution window, which
defeats the "real-time" requirement.

There are two honest ways to run this on Azure:

### Option A (recommended): Azure Container Apps or App Service (Always On)
Run `telegram_listener.py` as a long-running process inside a container or a
Linux App Service plan with "Always On" enabled. This preserves the
persistent connection Telethon needs and is the closest equivalent to running
it on a VPS, but with Azure's manageability (scaling rules, restart policies,
log streaming, managed identity for secrets).

Steps:
1. Build a Docker image (Dockerfile included in this package).
2. Push to Azure Container Registry.
3. Deploy to Azure Container Apps with `minReplicas: 1` so it never scales to
   zero, and restart policy `Always`.
4. Store secrets (API_HASH, webhook URL, shared secret) in Azure Key Vault
   and reference them as container app secrets/environment variables.
5. Mount a small persistent volume (Azure Files) for the Telethon `.session`
   file and the `logs/` directory, so the session survives restarts and you
   are not re-prompted for the login code each time.

### Option B: Azure Function with a Durable/long-polling pattern
If you specifically want to stay within the Functions ecosystem, use an
Azure Function App on a Premium or Dedicated plan (not Consumption) with
`functionTimeout` effectively unbounded, running an HTTP-triggered or
manually-started function that calls the same `main()` loop. This works but
loses most of the cost benefit of "serverless" Functions, since you are
paying for an always-on plan anyway — Option A is simpler for the same cost
profile.

**Bottom line:** for a true real-time listener, treat this as a small
always-on service (VPS, Container App, or App Service with Always On), not
a Consumption-plan Function. The code itself does not change between these
options — only how you host it.

## Dockerfile (for Option A)

See `Dockerfile` in this package. Build and push with:

```bash
docker build -t stockalerts:latest .
docker tag stockalerts:latest <your-acr-name>.azurecr.io/stockalerts:latest
docker push <your-acr-name>.azurecr.io/stockalerts:latest
```

Then deploy to Container Apps:

```bash
az containerapp create \
  --name stock-alert-listener \
  --resource-group <your-rg> \
  --environment <your-container-apps-env> \
  --image <your-acr-name>.azurecr.io/stockalerts:latest \
  --min-replicas 1 --max-replicas 1 \
  --env-vars TG_API_ID=secretref:tg-api-id TG_API_HASH=secretref:tg-api-hash \
             TG_PHONE_NUMBER=secretref:tg-phone POWER_AUTOMATE_URL=secretref:pa-url \
             WEBHOOK_SHARED_SECRET=secretref:webhook-secret TG_GROUP_ID=secretref:tg-group
```

## First-run login on a headless server

Telethon needs an interactive login the first time (SMS/Telegram code, plus
2FA password if enabled). On a headless container you cannot type into a
prompt, so generate the `.session` file once on your local machine by
running `python telegram_listener.py` locally, complete the login, then copy
the resulting `stock_alert_session.session` file into the deployment (or
onto the mounted Azure Files share). After that, the deployed instance
reuses the saved session and starts without any prompts.
