# KeyCRM to Trello

When a deal moves to another stage in a KeyCRM pipeline, the matching Trello card moves to the column with the same meaning.

## Problem

Sales lived in KeyCRM and delivery tracking lived in Trello. Someone was dragging the Trello card by hand after every stage change, and the two boards drifted.

## What was built

A sync job that reads pipeline cards from KeyCRM OpenAPI and moves Trello cards to the column named for that status.

Important detail: the reader uses `GET /v1/pipelines/cards`, not `/leads`. On the accounts this was built for, `/leads` answered 405. The pipeline id is `KEYCRM_PIPELINE_ID` in the env file. The map from KeyCRM status id to the exact Trello list name is `config/keycrm_status_map.json`. Keys are status ids. Values must match the Trello list title character for character.

## How it works

On each run the job loads cards for the configured pipeline, reads the current status id, looks up the Trello list name, and moves the card if it is not already there. A test folder is included for the mapping. Logs stay on the machine that runs the job.

## Stack

Python, KeyCRM OpenAPI, Trello API.

## Run locally

```bash
cp .env.example .env
```

Set the KeyCRM token, the pipeline id, and the Trello key, token, and board. Then run the sync entrypoint in `src/`. Adjust `config/keycrm_status_map.json` to your funnel before the first real run.

## What this public copy leaves out

`.env` and log files.
