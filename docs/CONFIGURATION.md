# Configuration reference

Copy `.env.example` to `.env` only for a local experiment. Blank values are
intentional. Secret and personal-identifier fields must never be committed.

| Variable | Required when | Sensitive? | Default | Consumer |
|---|---|---|---|---|
| `WHOOP_CLIENT_ID` | OAuth | application identifier | blank | `auth.py` |
| `WHOOP_CLIENT_SECRET` | OAuth | secret | blank | `auth.py` |
| `WHOOP_REDIRECT_URI` | OAuth | callback/config | blank | `auth.py` |
| `WHOOP_REFRESH_MARGIN_SECONDS` | OAuth | no | 60 in code | `whoop_auth.py` |
| `WHOOP_ENV_FILE` | operator handoff | private path | blank | `auth.py` |
| `WHOOP_TRANSFER_MARKER` | operator handoff | private path | blank | `token_handoff.py` |
| `TELEGRAM_BOT_TOKEN` | bot | secret | blank | `telegram_bot.py`, `send_reminder.py` |
| `TELEGRAM_CHAT_ID` | bot | personal identifier | blank | Telegram authorization |
| `TELEGRAM_USER_ID` | production bot | personal identifier | blank | sender authorization |
| `GEMINI_API_KEY` | direct AI | secret | blank | Gemini client |
| `GEMINI_MODEL` | direct/relay AI | provider choice | blank | AI client |
| `GEMINI_FALLBACK_MODELS` | AI fallback | provider choice | blank | AI client |
| `GEMINI_VISION_MODEL` | image AI | provider choice | blank | vision client |
| `GEMINI_VISION_FALLBACK_MODELS` | image AI fallback | provider choice | blank | vision client |
| `GEMINI_TRANSPORT` | relay AI | endpoint choice | blank | transport selector |
| `GEMINI_RELAY_URL` | relay AI | endpoint | blank | relay client |
| `GEMINI_RELAY_SECRET` | relay AI | secret | blank | relay client |
| `CONVERSATIONAL_ROUTER_ENABLED` | conversational AI | behavior flag | false | feature flags |
| `LEGACY_DESTRUCTIVE_TEXT_ENABLED` | legacy compatibility | behavior flag | false | feature flags |
| `CONVERSATION_MEMORY_ENABLED` | memory rollout | behavior flag | false | feature flags |
| `CONVERSATION_ANALYTICS_V2_ENABLED` | analytics rollout | behavior flag | false | feature flags |
| `BOUNDED_GEMINI_AGENT_ENABLED` | bounded AI | behavior flag | false | feature flags |
| `DAILY_FACTOR_CAPTURE_ENABLED` | factor extraction | behavior flag | false | feature flags |
| `WEEKLY_ANALYSIS_V2_ENABLED` | weekly analysis | behavior flag | false | feature flags |
| `GEMINI_VISION_ENABLED` | image analysis | privacy flag | false | Telegram image handler |
| `WHOOP_EVENING_CSV_URL` | legacy import | capability URL | blank | CSV importer |
| `VPS_HOST`, `VPS_IP` | deployment | infrastructure identifier | blank | deploy script |
| `VPS_PORT` | deployment | no | blank | deploy script |
| `VPS_USER` | deployment | account identifier | blank | deploy script |
| `VPS_PASSWORD` | deployment | secret | blank | deploy script |
| `VPS_SSH_KEY` | deployment | private path/key | blank | deploy script |
| `VPS_KNOWN_HOSTS` | deployment | trust material/path | blank | SSH client |
| `VPS_REMOTE_DIR` | deployment | private path | blank | deploy script |
| `WHOOP_SERVICE_USER` | deployment | account identifier | blank | service setup |
| `RELAY_BEARER_TOKEN` | relay service | secret | blank | relay app |
| `GEMINI_ALLOWED_MODELS` | relay service | provider choice | blank | relay app |
| `VERTEX_PROJECT_ID` | relay service | cloud identifier | blank | relay app |
| `RELAY_MAX_REQUEST_BYTES` | relay service | no | 8 MiB cap | relay app |
| `RELAY_MAX_IMAGE_BYTES` | relay service | no | 5 MiB cap | relay app |
| `RELAY_TIMEOUT_SECONDS` | relay service | no | 30, cap 60 | relay app |
| `PORT` | relay service | no | 8080 | relay app |

`MORNING_PIPELINE_RUN_ID`, `INVOCATION_ID`, test switches, and local platform
variables are runtime/internal values, not operator configuration. Unknown
boolean values fail closed. Production should require both Telegram chat and
sender IDs.
