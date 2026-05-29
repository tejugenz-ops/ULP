#!/usr/bin/env node
/**
 * Auto-creates 10 Railway worker services for the ULP bot.
 *
 * Prerequisites:
 *   1. A Railway API token → https://railway.app/account/tokens
 *   2. Your Railway Project ID → Project Settings → General → "Project ID"
 *   3. Your Railway Environment ID → Project Settings → Environments
 *      (usually called "production"; hover over it to copy the ID)
 *
 * Usage (run once):
 *   RAILWAY_TOKEN=xxx PROJECT_ID=xxx ENV_ID=xxx node scripts/create_workers.js
 *
 * What the script does:
 *   - Creates 10 services named ulp-worker-1 … ulp-worker-10
 *   - Connects each service to this GitHub repo (tejugenz-ops/ULP, branch: main)
 *   - Sets start command → python -m bot.worker_main
 *   - Sets WORKER_ID=1 … 10 as an environment variable on each service
 *
 * After the script finishes:
 *   - Go to each worker service in the Railway dashboard
 *   - Add a Volume → mount at /data
 *   - Trigger a deploy (the script does NOT trigger deploys automatically)
 */

"use strict";

const https = require("https");

// ── Config ────────────────────────────────────────────────────────────────────
const RAILWAY_TOKEN = process.env.RAILWAY_TOKEN;
const PROJECT_ID    = process.env.PROJECT_ID;
const ENV_ID        = process.env.ENV_ID;

const GITHUB_REPO   = "tejugenz-ops/ULP";
const GITHUB_BRANCH = "main";
const START_CMD     = "python -m bot.worker_main";
const WORKER_COUNT  = 10;
const RAILWAY_API   = "backboard.railway.app";
const API_PATH      = "/graphql/v2";

// ── Validation ────────────────────────────────────────────────────────────────
if (!RAILWAY_TOKEN || !PROJECT_ID || !ENV_ID) {
  console.error(`
  ✗ Missing required environment variables.

  Usage:
    RAILWAY_TOKEN=<token> PROJECT_ID=<project-id> ENV_ID=<env-id> node scripts/create_workers.js

  Where to find each value:
    RAILWAY_TOKEN  → https://railway.app/account/tokens  (create a new token)
    PROJECT_ID     → Railway dashboard → your project → Settings → General → "Project ID"
    ENV_ID         → Railway dashboard → your project → Settings → Environments → click the env name to copy its ID
  `);
  process.exit(1);
}

// ── GraphQL helper ────────────────────────────────────────────────────────────
function gql(query, variables = {}) {
  return new Promise((resolve, reject) => {
    const body = JSON.stringify({ query, variables });
    const req = https.request(
      {
        hostname: RAILWAY_API,
        path: API_PATH,
        method: "POST",
        headers: {
          "Content-Type":   "application/json",
          "Authorization":  `Bearer ${RAILWAY_TOKEN}`,
          "Content-Length": Buffer.byteLength(body),
        },
      },
      (res) => {
        let data = "";
        res.on("data", (c) => (data += c));
        res.on("end", () => {
          try {
            const json = JSON.parse(data);
            if (json.errors) reject(new Error(JSON.stringify(json.errors)));
            else resolve(json.data);
          } catch (e) {
            reject(new Error(`Non-JSON response: ${data}`));
          }
        });
      }
    );
    req.on("error", reject);
    req.write(body);
    req.end();
  });
}

// ── Railway mutations ─────────────────────────────────────────────────────────

async function createService(name) {
  const data = await gql(
    `mutation ServiceCreate($input: ServiceCreateInput!) {
       serviceCreate(input: $input) { id name }
     }`,
    {
      input: {
        projectId: PROJECT_ID,
        name,
        source: { repo: GITHUB_REPO, branch: GITHUB_BRANCH },
      },
    }
  );
  return data.serviceCreate;
}

async function setStartCommand(serviceId) {
  await gql(
    `mutation ServiceInstanceUpdate($serviceId: String!, $environmentId: String!, $input: ServiceInstanceUpdateInput!) {
       serviceInstanceUpdate(serviceId: $serviceId, environmentId: $environmentId, input: $input)
     }`,
    {
      serviceId,
      environmentId: ENV_ID,
      input: { startCommand: START_CMD },
    }
  );
}

async function setVariable(serviceId, name, value) {
  await gql(
    `mutation VariableUpsert($input: VariableUpsertInput!) {
       variableUpsert(input: $input)
     }`,
    {
      input: {
        projectId:     PROJECT_ID,
        serviceId,
        environmentId: ENV_ID,
        name,
        value,
      },
    }
  );
}

// ── Main ──────────────────────────────────────────────────────────────────────

async function main() {
  console.log(`\n🚀 Creating ${WORKER_COUNT} worker services in Railway…\n`);

  const results = [];

  for (let i = 1; i <= WORKER_COUNT; i++) {
    const name = `ulp-worker-${i}`;
    process.stdout.write(`  [${i}/${WORKER_COUNT}] ${name} … `);

    try {
      // 1. Create the service (with GitHub source)
      const service = await createService(name);

      // 2. Set start command
      await setStartCommand(service.id);

      // 3. Set WORKER_ID variable
      await setVariable(service.id, "WORKER_ID", String(i));

      console.log(`✓  (service id: ${service.id})`);
      results.push({ name, id: service.id, ok: true });
    } catch (err) {
      console.log(`✗  ERROR: ${err.message}`);
      results.push({ name, ok: false, error: err.message });
    }
  }

  // ── Summary ──────────────────────────────────────────────────────────────
  const ok   = results.filter((r) => r.ok);
  const fail = results.filter((r) => !r.ok);

  console.log(`\n──────────────────────────────────────────`);
  console.log(`  Done: ${ok.length} created, ${fail.length} failed`);

  if (fail.length) {
    console.log(`\n  Failed services:`);
    fail.forEach((r) => console.log(`    • ${r.name}: ${r.error}`));
  }

  console.log(`
  ✅ Next steps for each worker in Railway dashboard:
     1. Open the service → Settings → Volumes
     2. Add a volume → mount path: /data
     3. Trigger a redeploy (Deploy button)

  ℹ️  The bot service (WORKER_ID=0) already handles its own deploy.
  `);
}

main().catch((err) => {
  console.error("\nFatal error:", err.message);
  process.exit(1);
});
