# Phase 5 — deployment

## Instruction given

No full prompt. The scope came from the phase list:

> | 5 | Deployment |

the Phase 1 out-of-scope note ("deployment config"), and a comment in `.env`:

> `GitHub_repo_Link=https://github.com/0DevDutt0/BrightCare-Clinic-Bot #for CI/CD pipeline`

followed by:

> go ahead with phase 5 from the outline

## What was built

| artefact | purpose |
|---|---|
| `Dockerfile` | host-agnostic image; runs on Render, Railway, Fly, anything |
| `.dockerignore` | keeps `.env` and `credentials/` out of every image layer |
| `render.yaml` | one worked host blueprint, so deploying is a click not a project |
| `.github/workflows/ci.yml` | tests, credential scan, image build and smoke test |
| README `## Deployment` | the chicken-and-egg step, the worker count, the CI jobs |

## Assumptions made

1. **Host-agnostic first, one worked example second.** A `Dockerfile` runs anywhere;
   `render.yaml` exists so there is a concrete path rather than a shrug. Choosing
   Render was a guess — swapping to Railway or Fly needs no code change, only a
   different manifest.

2. **`render.yaml` picks a *web service*, therefore `RUN_MODE=webhook`.** On a free
   tier the service sleeps when idle, and a sleeping process cannot poll. The inbound
   webhook request is what wakes it. On an always-on plan or a background worker,
   polling is simpler and the manifest should say `polling`.

3. **`--workers 1` is pinned, not defaulted.** Conversation state is in memory, so a
   second worker is a second process with its own view of every conversation: a user
   could be asked for their email by one worker and reply to another that has never
   heard of them. Raising it is safe only once the Redis backend exists.

4. **CI needs no secrets.** Every external service is faked in the suite, so the test
   job runs on a bare checkout. If a test ever starts needing a live key, that job is
   where it fails loudly rather than being quietly skipped.

5. **A credential scan belongs in CI, not in a local git hook.** A hook lives on one
   machine and can be bypassed with `--no-verify`; CI cannot. This was offered twice
   as a local hook and declined, so it went where it is enforceable and where it
   changes nothing about the developer's machine.

6. **Nothing is pushed automatically.** The workflow builds and tests; it does not
   deploy. `autoDeploy: true` in `render.yaml` means Render deploys on push once the
   repo is connected — the trigger is the push, and the push is a human decision.

## Two things testing changed

**The credential scan false-positived on its own test fixture.** `tests/test_config.py`
legitimately contains `-----BEGIN PRIVATE KEY-----\nnotarealkey`, and the first pattern
matched the marker alone. A scanner that fails on valid code is a scanner someone
disables by Friday, so the pattern now requires 40+ characters of key material after
the marker. Verified both ways: it still catches a copy of the real `.env`, and no
longer trips on the fixture.

**The image smoke test used a placeholder private key, which cannot work.** Config
resolves credentials eagerly at startup and google-auth rejects anything that is not a
loadable PEM — the container would have crash-looped and the CI job would have failed
on every run. CI now generates a throwaway RSA key with `openssl`. Caught by building
and running the image locally before trusting the workflow.

## Verified locally

Webhook mode, never previously exercised, driven directly:

```
/health                     -> {"run_mode":"webhook","polling_active":false}
POST .../webhook/wrong      -> 200, logged webhook.bad_secret, not processed
POST .../webhook/<secret>   -> 200, classified faq/location, handled in 1629 ms
POST malformed body         -> 200, logged webhook.invalid_json
```

All three answer 200, as Telegram requires.

Container:

```
/health          200, {"status":"ok","run_mode":"polling"}
docker exec id   uid=1000(clinic)      -- not root
/app contents    app/ requirements.txt -- no .env
```

Telegram answered 401 to the fake token throughout and the app kept serving, which is
the Phase 1 intent holding up in a container: a bad token should look like a running
service with a visible error, not a crash loop.

Full suite with `.env` renamed away, as CI sees it: **327 passed**.

## Not done, and why

**Nothing has been pushed to GitHub.** The remote is configured and 18 commits are
waiting, but publishing a repository is a decision for its owner, not a step an agent
takes on its own.

**No Redis.** Deployment is what makes the in-memory store's limits bite — free tiers
restart often, and an in-flight booking does not survive it. A *completed* booking is
safe, since it lives on the calendar. The `StateStore` interface has been async since
Phase 1 precisely so this swap changes no handler code.

**No staging environment, no rollback.** For a take-home the deploy is one service; a
real clinic would want at least a staging deploy before production traffic.
