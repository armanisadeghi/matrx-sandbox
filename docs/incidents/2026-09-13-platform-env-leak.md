# Incident 2026-09-13 — platform master credentials in every sandbox

**Status:** class closed in code (orchestrator commit named at the bottom); rotation OPEN — operator action.

## What happened

`orchestrator/sandbox_manager.py` (create path) forwarded every env var whose NAME appears in
(a) the file at `settings.aidream_passthrough_env_file` (`/srv/projects/aidream/.env` on the hosted
tier; whatever the EC2 host points it at) and (b) the explicit `settings.aidream_passthrough_env`
list in `orchestrator/config.py`, from the orchestrator's process environment into the container
env of EVERY sandbox. Only the path-shape overrides beneath the loop were gated on
`template == "aidream"`; the loop itself was not. The migration refresh
(`migrate._refresh_platform_environment`) re-applied the same registry on every migrated box.

Proof: sandbox `sbx-18796e7c90d6`, template `slim`, tier `ec2`, created 2026-09-13 05:46 — the
agent ran `env` and read `MATRX_DATABASE_URL` (full pooler URL with password), `ADMIN_AUTH_TOKEN`,
`AIDREAM_SANDBOX_SERVICE_TOKEN`, `GITHUB_CLIENT_SECRET`, `SHOPIFY_API_ACCESS_TOKEN`,
`CEREBRAS_API_KEY_PERSONAL` and more. At that moment 229 live boxes across 218 users carried
the platform's master credentials. Every sandbox created since the passthrough shipped must be
treated as having exposed every name below to its user.

## The fix (class, not instance)

1. **Template gate.** `platform_passthrough_env()` returns nothing unless the template is in
   `PLATFORM_PASSTHROUGH_TEMPLATES` (`{"aidream"}`). Every other template's env is exactly: the
   orchestrator-managed identity/storage vars set explicitly in `create_sandbox`, the caller's
   `config.env`, and the user's vault secrets. The registry is not consulted at all.
2. **Deny-list.** Even for `aidream`, names matching `MASTER_CREDENTIAL_PATTERNS`
   (`*PASSWORD*`, `*_SECRET*`, `*SECRET_*`, `*DATABASE_URL*`, `*CONNECTION_STRING*`,
   `*_SERVICE_TOKEN*`, `ADMIN_*TOKEN*`, `*_API_KEY*`, `*_API_TOKEN*`, `*_ACCESS_TOKEN*`,
   `*_AUTH_TOKEN*`, `*_ACCESS_KEY*`, `*_PRIVATE_KEY*`, `*CREDENTIALS*`, `*_PAT`) are withheld
   unless the knob `infrastructure.sandbox.aidream_template_forwards_master_credentials`
   (default OFF, seeded live 2026-09-13, record: aidream `db/migrations/0669_…`) is on. A missing
   row is OFF (`knobs.security_knob_bool`, loud). Vault secrets and `config.env` are never filtered.
3. **Migration refresh** obeys both rules and STRIPS previously leaked registry names from a
   non-aidream box — a leaked box is cleaned by its next migration/recreate.
4. **Honest diagnostics.** `GET /sandboxes/{id}/diagnostics` now reports
   `platform_env_leaked_count/names` (registry names present that a fresh box of that template
   would not receive); `/health` names the templates and the knob.
5. **Guard:** `orchestrator/tests/test_platform_env_isolation.py` — failed 31/33 against the old
   code (slim + development creates received the platform env), passes after.

## Rotation list — NAMES ONLY, never values

Every name below was a passthrough name and must be assumed exposed. The union is the explicit
list in `orchestrator/config.py` (120 names) plus the keys of aidream's `.env` (204 keys, read
from the maintainer's checkout — the deployed hosts' files may hold MORE names: on each
orchestrator host run `grep -oE '^[A-Za-z_][A-Za-z0-9_]*' $(the passthrough file) | sort -u` and
diff against this list. `MATRX_DATABASE_URL` was observed live but is NOT in this union, which
proves at least one host file carries names beyond it.)

Also observed live and not in the union: `MATRX_DATABASE_URL` (orchestrator's own platform DB URL).

Legend: `D` = caught by the deny-list today; `[explicit]` = named in config.py's list.
Rotate the `D` rows first (credentials); the others are identifiers/config, still worth reading
through for anything sensitive that the patterns miss.

```
D ADMIN_API_TOKEN  [explicit]
D ADMIN_AUTH_TOKEN  [explicit]
  ADMIN_PYTHON_ROOT
  ADMIN_SAVE_DIRECT_ROOT
  ADMIN_TS_ROOT
  ADMIN_USER_ID  [explicit]
  AGENT_USER_ID
D AHREFS_MATRIX_API_KEY  [explicit]
D AIDREAM_SANDBOX_SERVICE_TOKEN  [explicit]
  AIMATRX_AIDREAM_REDIRECT_URI  [explicit]
  AIMATRX_OAUTH_CLIENT_ID  [explicit]
D AI_ADMIN_PASSWORD
  AI_ADMIN_USERNAME
  ALLOWED_HOSTS  [explicit]
D AME_GOOGLE_SEARCH_API_KEY  [explicit]
D ANTHROPIC_ADMIN_API_KEY
D ANTHROPIC_API_KEY  [explicit]
  ANTHROPIC_KEY  [explicit]
D AWS_ACCESS_KEY_ID
  AWS_BUCKET_MODELS  [explicit]
  AWS_REGION  [explicit]
  AWS_S3_DEFAULT_BUCKET
  AWS_S3_PUBLIC_BUCKET
D AWS_SECRET_ACCESS_KEY
  BASE_DIR
  BING_WEBMASTER_OAUTH_CLIENT_ID
D BING_WEBMASTER_OAUTH_CLIENT_SECRET
  BING_WEBMASTER_OAUTH_REDIRECT_URI
D BRAVE_SEARCH_API_KEY  [explicit]
D BRAVE_SEARCH_API_KEY_AI  [explicit]
D BRAVE_SEARCH_API_KEY_PRO_AI  [explicit]
  BROKER_TOKEN_SIGNING_KEY
D CARTESIA_API_KEY  [explicit]
  CDN_PUBLIC_BASE_URL
D CEREBRAS_API_KEY  [explicit]
D CEREBRAS_API_KEY_PERSONAL  [explicit]
  CHAT_TIME_CHECK_ENABLED
D CIVIT_API_KEY  [explicit]
D CLOUDFLARE_API_TOKEN
  CLOUDFLARE_ZONE_ID
D CLOUD_FILES_BYPASS_SECRET
D COHERE_API_KEY  [explicit]
D COOLIFY_API_TOKEN
  COOLIFY_API_URL
  COOLIFY_APP_UUID
D COURTLISTENER_API_TOKEN
D CREDENTIALS_ENCRYPTION_KEY
  DATACENTER_PROXIES
  DATA_FOR_SEO_EMAIL  [explicit]
D DATA_FOR_SEO_PASSWORD  [explicit]
  DEBUG  [explicit]
  DEVELOPER_USER_ID  [explicit]
D DEV_LOGIN_SECRET
D DOCKER_FULL_ACCESS_TOKEN
D ELEVENLABS_API_KEY  [explicit]
  FIREBASE_SERVICE_ACCOUNT  [explicit]
D FIREWORKS_API_KEY  [explicit]
D FOUND_OLD_DJANGO_SECRET_KEY
  FRONTEND_BASE_URL
D GEMINI_API_KEY  [explicit]
D GETIMG_API_KEY  [explicit]
  GITHUB_BOT_ACCOUNT_USERNAME  [explicit]
  GITHUB_BOT_EMAIL  [explicit]
  GITHUB_CLIENT_ID  [explicit]
D GITHUB_CLIENT_SECRET  [explicit]
  GITHUB_ORG_NAME  [explicit]
D GITHUB_PAT  [explicit]
D GITHUB_WEBHOOK_SECRET
  GOOGLE_AI_STUDIO  [explicit]
D GOOGLE_API_KEY  [explicit]
D GOOGLE_APPLICATION_CREDENTIALS  [explicit]
  GOOGLE_CLIENT_ID
D GOOGLE_CLIENT_SECRET
D GOOGLE_OAUTH_CLIENT_SECRETS  [explicit]
D GOOGLE_PSI_API_KEY
D GOOGLE_SEARCH_API_KEY  [explicit]
  GOOGLE_SEARCH_CSE_ID  [explicit]
D GROQ_API_KEY  [explicit]
D GUEST_FINGERPRINT_SECRET
  HEALTH_CHECK_URL
  HOSTINGER_NEW_SSH
  HOSTINGER_NEW_SSL_KEY
D HOSTINGER_SERVER_ROOT_NEW_PASSWORD
D HOSTINGER_SERVER_ROOT_PASSWORD
  HUGGING
D HUGGINGFACE_API_KEY  [explicit]
  HUGGING_FACE_TOKEN_ID  [explicit]
  LOCAL_USER_ID  [explicit]
  LOG_LEVEL  [explicit]
  LULU_API_BASE
  LULU_CLIENT_KEY
D LULU_CLIENT_SECRET
D MAILGUN_API_KEY  [explicit]
D MATRX_ACCESS_TOKEN_SECRET
D MATRX_DM_DIRECT_CONNECTION_STRING  [explicit]
  MATRX_DM_HOST  [explicit]
  MATRX_DM_NAME  [explicit]
D MATRX_DM_PASSWORD  [explicit]
  MATRX_DM_PORT  [explicit]
  MATRX_DM_PROJECT_URL  [explicit]
  MATRX_DM_PROTOCOL  [explicit]
  MATRX_DM_PUBLISHABLE_KEY  [explicit]
D MATRX_DM_SECRET_KEY  [explicit]
  MATRX_DM_USER  [explicit]
  MATRX_ENGINE_SENTRY_DSN
  MATRX_ENGINE_TENSOR_DOCK_SERVER_KEY
  MATRX_ENGINE_TENSOR_DOCK_URL
  MATRX_ENV  [explicit]
  MATRX_ORM_CACHE_DEBUG
  MATRX_PYTHON_ROOT
  MATRX_REDACTION_KMS_KEY_ID
  MATRX_ROLE
  MATRX_SAVE_DIRECT
  MATRX_SCRAPER_TOKEN
  MATRX_SCRAPER_URL
  MATRX_STAGE
  MATRX_TOOL_DEBUG_DB_DISABLED
  MATRX_TOOL_DEBUG_LOG_DISABLED
  MATRX_TOOL_DEBUG_VERBOSE
  MATRX_TS_ROOT
  MATRX_VERBOSE_ASYNCPG
D MISTRAL_API_KEY  [explicit]
D MODEL_LABS_API_KEY  [explicit]
D MONGO_API_KEY  [explicit]
D MONGO_API_KEY_NAME  [explicit]
D MONGO_PASSWORD  [explicit]
  MONGO_URI  [explicit]
  MONGO_USERNAME  [explicit]
D MOONSHOT_API_KEY
D NEWS_API_KEY  [explicit]
  NEXT_PUBLIC_SLACK_CLIENT_ID
  NEXT_PUBLIC_SLACK_REDIRECT_URL
D NOT_WORKING_ELEVENLABS_API_KEY
D OPENAI_API_KEY  [explicit]
D PAGESPEED_INSIGHTS_API_KEY  [explicit]
D PIONEER_API_KEY
D PLAYHT_SECRET_KEY  [explicit]
  PLAYHT_USER_ID  [explicit]
  POSTGRES_DB  [explicit]
  POSTGRES_HOST  [explicit]
D POSTGRES_PASSWORD  [explicit]
  POSTGRES_PORT  [explicit]
  POSTGRES_PROTOCOL  [explicit]
  POSTGRES_USER  [explicit]
  PROJECT_ID  [explicit]
  PUBLIC_URL
  PYTHONPATH
D RAPID_API_KEY  [explicit]
D REPLICATE_API_TOKEN  [explicit]
D RUNPOD_API_KEY  [explicit]
D SAMBA_NOVA_API_KEY  [explicit]
D SANDBOX_ORCHESTRATOR_API_KEY
  SANDBOX_ORCHESTRATOR_URL
D SERPAPI_API_KEY  [explicit]
D SERVER_PASSWORD
  SERVER_USER
D SERVER_aidreamuser_PASSWORD
D SHOPIFY_API_ACCESS_TOKEN  [explicit]
D SHOPIFY_API_SECRET_KEY  [explicit]
  SHOPIFY_APP_CLIENT_ID  [explicit]
D SHOPIFY_APP_CLIENT_SECRET  [explicit]
  SHOPIFY_APP_NAME  [explicit]
  SLACK_CLIENT_ID  [explicit]
D SLACK_CLIENT_SECRET  [explicit]
  SLACK_REDIRECT_URL  [explicit]
D SPEECHMATICS_API_KEY  [explicit]
D SPEECHMATICS_AUTH_TOKEN  [explicit]
D STABILITY_API_KEY  [explicit]
D STRIPE_PRINT_WEBHOOK_SECRET
D STRIPE_SECRET_KEY
D SUPABASE_ACCESS_TOKEN
D SUPABASE_AUTOMATION_MATRIX_DB_PASSWORD  [explicit]
  SUPABASE_CMS_DATABASE_NAME
D SUPABASE_CMS_DATABASE_URL
  SUPABASE_CMS_HOST
D SUPABASE_CMS_PASSWORD
  SUPABASE_CMS_PORT
  SUPABASE_CMS_PROTOCOL
  SUPABASE_CMS_PUBLISHABLE_KEY
D SUPABASE_CMS_SECRET_KEY
  SUPABASE_CMS_URL
  SUPABASE_CMS_USER
  SUPABASE_DJANGO_KEY  [explicit]
  SUPABASE_DJANGO_URL  [explicit]
  SUPABASE_KEY  [explicit]
  SUPABASE_MATRIX_DATABASE_NAME  [explicit]
  SUPABASE_MATRIX_HOST  [explicit]
D SUPABASE_MATRIX_JWT_SECRET  [explicit]
  SUPABASE_MATRIX_KEY  [explicit]
  SUPABASE_MATRIX_NAME  [explicit]
  SUPABASE_MATRIX_OAUTH_ISSUER_URL
D SUPABASE_MATRIX_PASSWORD  [explicit]
  SUPABASE_MATRIX_PORT  [explicit]
  SUPABASE_MATRIX_PROTOCOL  [explicit]
  SUPABASE_MATRIX_PUBLISHABLE_KEY
D SUPABASE_MATRIX_SECRET_KEY
  SUPABASE_MATRIX_URL  [explicit]
  SUPABASE_MATRIX_USER  [explicit]
D SUPABASE_NEW_PASSWORD
D SUPABASE_SAMPLE_DB_PASSWORD  [explicit]
  SUPABASE_URL  [explicit]
D SUPABASE_WEBHOOK_SECRET
  SYSTEM_USER_ID  [explicit]
  TENSORDOCK_AUTH_KEY
D TENSORDOCK_AUTH_TOKEN
  TEST_ADMIN_USER_ID
  TEST_CONVERSATION_ID
  TEST_ORGANIZATION_ID
  TEST_PROJECT_ID
  TEST_USER_EMAIL
  TEST_USER_ID
D TOGETHER_API_KEY  [explicit]
  TOOL_WORKSPACE_BASE  [explicit]
  TWILIO_ACCOUNT_SID  [explicit]
D TWILIO_AUTH_TOKEN  [explicit]
  TWILIO_MESSAGING_SERVICE_SID  [explicit]
  TWILIO_PHONE_NUMBER  [explicit]
  TWILIO_SKIP_VALIDATION  [explicit]
  TWILIO_VERIFY_SID  [explicit]
D UNSPLASH_ACCESS_KEY
D UNSPLASH_SECRET_KEY
D VOYAGE_API_KEY
D XAI_API_KEY  [explicit]
D YOUTUBE_DATA_API_KEY
```

Also rotate, though it is set by the orchestrator itself and not part of the registry:
`MATRX_AIDREAM_SERVICE_TOKEN` (= aidream's `AIDREAM_SANDBOX_SERVICE_TOKEN`) — still handed to
every box because the in-container daemon needs it (see below). Rotating it rotates the
`AIDREAM_SANDBOX_SERVICE_TOKEN` row above.

Runbooks: platform Postgres / Supabase — aidream `docs/`; orchestrator keys —
[OPERATIONS.md](../OPERATIONS.md) § Rotating the hosted-tier API key. After rotation, migrate or
recreate every box born before the fix: the diagnostics endpoint's `platform_env_leaked_count`
is the census of what is still dirty.

## What is still open (not this fix)

- **The bridge token is a shared master, not sandbox-scoped.** Every box still receives
  `MATRX_AIDREAM_SERVICE_TOKEN`; it is the ONE shared `AIDREAM_SANDBOX_SERVICE_TOKEN` and aidream
  trusts `X-Matrx-User-Id` beside it (`aidream/api/routers/cloud_files_bridge.py`,
  `user_secrets.py`, `google_integrations.py`, github access-token). A box holding it can act as
  ANY user against cloud-files, the secrets vault and the GitHub token endpoint. The daemon
  needs a token (`sandbox-image/sdk/matrx_agent/cloud_sync/client.py`, `cli/files.py`,
  `scripts/matrx-git-credential-env`, `configure-git-credentials.sh`, `cloud-files-sync.sh`,
  `matrx_tools/browser_manager.py`) — the fix is a per-sandbox scoped token minted by aidream
  (sandbox_id + user_id + org bound, revocable), the way `MATRX_AGENT_TOKEN` is already
  per-sandbox. Separate campaign; filed with this note.

## REOPENED and closed differently — 2026-09-18 (XT-10, feedback 34dcf28a)

The fix above was a **pattern denylist**, and a denylist is fail-open by construction.
Measured on a real hosted box (`sbx-7bc1060b325f`, created as admin@admin.com, row
`6e4c88ce-b38d-4b6c-ae0a-3700917f2a71`) by reading its own `agent-env`: the orchestrator
reported `denied_count=72` and the box looked protected, while **14 secret-shaped names were
forwarded** because no pattern matched their exact spelling —

`ANTHROPIC_KEY` (a live `sk-ant-api03-…`), `MATRX_AGENT_TOKEN`, `MATRX_SCRAPER_TOKEN`,
`SUPABASE_KEY`, `SUPABASE_MATRIX_KEY`, `SUPABASE_DJANGO_KEY`, `SUPABASE_MATRIX_DJANGO_KEY`,
`SUPABASE_AI_MATRIX_KEY`, `SUPABASE_SAMPLE_MATRIX_KEY`, `SUPABASE_MATRIX_PUBLISHABLE_KEY`,
`TENSORDOCK_AUTH_KEY`, `MATRX_ENGINE_TENSOR_DOCK_SERVER_KEY`, `HUGGING_FACE_TOKEN_ID`,
`MATRX_REDACTION_KMS_KEY_ID`.

`.*_API_KEY.*` does not match `ANTHROPIC_KEY`; `.*_SERVICE_TOKEN.*` does not match
`MATRX_AGENT_TOKEN`; a Supabase service-role key is spelled `..._KEY`, not `..._SECRET`. The
box also held `SUPABASE_MATRIX_HOST`/`_PORT`/`_USER`/`_DATABASE_NAME` — every part of a
platform DB connection except the password.

**The fix, by class not instance:** the forward set is now the FAIL-CLOSED
`sandbox_manager.PLATFORM_ENV_ALLOWLIST`. A name is forwarded only if it is on it; everything
else is dropped, counted and NAMED in the boot report (`platform env withheld …` at WARNING,
and `config.platform_env` on the row). `MASTER_CREDENTIAL_PATTERNS` survives ONLY as a
secondary guard — `_assert_allowlist_holds_no_secret_shapes()` refuses at import and on every
decision if an allowlisted name is secret-shaped. **Never fix a missing name by adding a
pattern.**

Applied at every place env reaches a container, all of which funnel through
`platform_env_decision`: `create_sandbox` (docker run), `migrate._refresh_platform_environment`
(→ `migrate.py` and `hosted_runtime.migrate_hosted`), the `/diagnostics` expectation, and
`/aidream-passthrough`. `vault_env_refresh.leaked_platform_names` now sweeps the **aidream
template too** (it used to skip it wholesale, which is why a box born before this kept a live
`ANTHROPIC_KEY` in its shell until it was destroyed) — protecting the person's own vault names,
`ORCHESTRATOR_MANAGED_ENV`, and the orchestrator's own path overrides
(`aidream_template_path_overrides`, extracted so the setter and the sweep cannot drift).

**And the disclosure channel:** `GET /sandboxes/{id}/agent-env` returned every VALUE verbatim
"because operator-only" — that is how the live key was read out over HTTP. It now answers
`{key, present, chars, redacted}`. Names only, never values — the same rule the binding report
already followed. The frontend diagnostics panel renders presence and length.

Guards: `orchestrator/tests/test_platform_env_allowlist.py` — 18 tests, 15 proven RED on the
pre-fix code then green, including the 14 names planted in the host env and asserted absent
from the rendered container env, and an unknown name (`BRAND_NEW_PROVIDER_CREDENTIAL`) withheld
by default.

### Round 2 — what the independent verify (V-XT-10) refuted, and the real close

The allowlist above was right and measured, but the first pass shipped four holes. A verifier that
did not inherit the builder's file list found them by running the real code against admin's REAL
pre-fix boxes:

1. **Only the passthrough branch had been converted.** `leaked_platform_names`' NON-passthrough
   branch was still the pattern denylist — and `bare` / `slim` boxes are exactly what this incident
   contaminated. Run against admin's live `bare` box `sbx-7520dde5030e` (born 2026-08-04, 174 names)
   it cleared 67 and LEFT `ANTHROPIC_KEY`, `MATRX_SCRAPER_TOKEN`, `SUPABASE_KEY`,
   `SUPABASE_MATRIX_KEY`, `SUPABASE_DJANGO_KEY`, `SUPABASE_AI_MATRIX_KEY`,
   `SUPABASE_MATRIX_DJANGO_KEY`, `SUPABASE_SAMPLE_MATRIX_KEY`, `TENSORDOCK_AUTH_KEY`,
   `HUGGING_FACE_TOKEN_ID`. **The fix had improved the branch that was least affected.**
   → There is now ONE census, `vault_env_refresh.unentitled_platform_env_names`, and it judges
   every template by the same rule: entitled = the person's vault ∪ `ORCHESTRATOR_MANAGED_ENV` ∪
   the allowlist ∪ (aidream's path overrides); everything else the box holds from the platform's own
   environment — the registry, the retired git names, any master-credential shape — is cleared.
2. **The secondary guard was itself a denylist.** It used `is_master_credential_name`, so it did not
   fire for `ANTHROPIC_KEY`, `SUPABASE_SERVICE_ROLE_KEY`, `OPENAI_KEY` or `MATRX_AGENT_TOKEN` on the
   allowlist — the exact names that leaked — and its own test passed only because it used
   `OPENAI_API_KEY`, which a pattern happens to catch. Worse, the runtime re-check was called with
   no argument, binding the def-time default, so widening the module global forwarded
   `ADMIN_API_TOKEN`'s real value while the comment claimed "a runtime patch cannot slip past it".
   → The guard is now POSITIVE: an entry must be in `PLATFORM_ENV_PUBLIC_BASICS` or end in `_URL`
   with a bare `http(s)` value, the HOST's actual value is read as well (a secret can be spelled
   innocently), and the live global is passed on every decision.
3. **A sweep can never clean a box, and the docs implied it could.** `Config.Env` and
   `/proc/1/environ` cannot be rewritten in place, and the sweep defers entirely on a busy box. The
   binding report now carries `leaked_platform_env_still_in_container_environ` and the remedy beside
   the claim, `GET /platform-env-census` lists every contaminated box fleet-wide, and the cure is
   `POST /sandboxes/{id}/migrate` — it recreates the container from the same home volume and keeps
   the `sandbox_id`, so existing bindings stay valid.
4. **The diagnostic cried wolf.** `platform_env_leaked_*` subtracted neither the person's vault nor
   `ORCHESTRATOR_MANAGED_ENV` and reported seven "leaks" on two clean post-fix boxes, disagreeing
   with the sweep. It is now `platform_env_unentitled_*`, computed by the one census function.
5. **`runtime_env_error` returned the raw `env` output** on a non-zero exit — the redaction with a
   hole in it. Both error paths now report the exit code and withhold the output.

**And the honest part.** "Secrets never reach the container" was false as worded, and is still false:
`create_sandbox` writes `MATRX_AIDREAM_SERVICE_TOKEN` — the ONE shared
`AIDREAM_SANDBOX_SERVICE_TOKEN`, with aidream trusting `X-Matrx-User-Id` beside it — plus the
hosted-tier `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` into every box through
`ORCHESTRATOR_MANAGED_ENV`, which the allowlist does not govern. They are entitled today because the
daemon and hot-sync need them. **That is an open item** (per-box scoped tokens; see § What is still
open) and the census says so in its own response rather than leaving a reader to infer a clean box.
