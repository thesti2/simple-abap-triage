# Slack setup (Phase 2)

Works on the **free Slack plan**. You need a Slack workspace where you are allowed to install apps
(your own workspace, or ask the workspace admin). Takes about 10 minutes.

All values below go into the git-ignored `.env` only – never into markdown or committed files.

## 1. Create the app
1. Open https://api.slack.com/apps → **Create New App** → **From a manifest**.
2. Pick the workspace, choose **YAML**, paste [`slack-app-manifest.yaml`](slack-app-manifest.yaml) → **Create**.

## 2. Collect the credentials
| Where in the app settings | Value | `.env` key |
|---|---|---|
| *Basic Information* → App Credentials → **Signing Secret** | 32-char hex | `SLACK_SIGNING_SECRET` |
| *Basic Information* → App-Level Tokens → **Generate Token and Scopes**, scope `connections:write` | `xapp-…` | `SLACK_APP_TOKEN` (Socket Mode only) |
| *Install App* → **Install to Workspace** → **Bot User OAuth Token** | `xoxb-…` | `SLACK_BOT_TOKEN` |

## 3. Create the triage channel
1. Create a channel, e.g. private `#abap-triage`, and add the people who handle alerts
   (for now anyone in the channel may approve a TR release).
2. In the channel: `/invite @abap-triage`.
3. Channel name → *About* (bottom) → **Channel ID** `C…` → `.env` key `SLACK_CHANNEL_ID`.

## 4. Switch to HTTP mode (after the backend runs on Cloud Run)
Socket Mode is for local development. For Cloud Run, in the app settings:
1. *Socket Mode* → turn **off**.
2. *Event Subscriptions* → Request URL `https://<CLOUD_RUN_URL>/slack/events` (Slack sends a challenge; it must show *Verified*).
   Bot events stay: `app_mention`, `message.channels`, `message.groups`.
3. *Interactivity & Shortcuts* → Request URL `https://<CLOUD_RUN_URL>/slack/interactions`.
4. Put `SLACK_BOT_TOKEN` and `SLACK_SIGNING_SECRET` into Secret Manager (see `infra/terraform/README.md` step 4)
   and `slack_channel_id` into `terraform.tfvars`.

`<CLOUD_RUN_URL>` = `terraform output cloud_run_url`.

## How the bot uses Slack
- Every alert = **one parent message** (card: category, root cause, recommendation, buttons *Fix it* / *Approve* / *Reject*).
  Everything about that alert happens in its **thread**.
- Ask questions by mentioning the bot in the thread: `@abap-triage what is the root cause?`
- The fix result (RCA + before/after test document) is uploaded into the same thread.
