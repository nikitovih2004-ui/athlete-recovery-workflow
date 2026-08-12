# Project story

This project is a public-readable account of an evolving personal engineering
system. It is deliberately a sanitized narrative, not a dump of the original
private development history.

## Stage 1 — local prototype

The first useful shape was a local Python program with a small SQLite store and
deterministic scripts. The goal was simple: fetch wearable records, normalize
dates, and make a morning view understandable without a hosted service. At this
stage there was no remote deployment, network-facing dashboard, or chat surface.

The important design decision from this stage survived: the database is the
canonical source, while reports and HTML are generated views.

## Stage 2 — wearable data and a canonical read model

WHOOP recovery, sleep, and workout records introduced provider schemas,
time-zone boundaries, incomplete days, retries, and evolving raw fields. The
pipeline gained explicit ingestion and reconciliation code, a canonical read
model, and integrity checks. Raw provider responses remain data, not executable
instructions.

## Stage 3 — authorized messaging flow

An optional private Telegram interface made it possible to record supplements,
manual workouts, daily context, and corrections from a phone. The flow evolved
from direct commands toward reserved, idempotent actions with explicit
validation, confirmation, provenance, and delivery state.

The public export keeps the boundary clear: only an operator-configured private
chat is in scope, and tests use fake messages and fake providers.

## Stage 4 — deployment and reliability

Scheduled morning work introduced a VPS service, backup/restore concerns,
deployment manifests, writer suspension, rollback gates, and durable pipeline
observability. The production-ready code line is the source for this export;
private operational evidence, live infrastructure identifiers, and incident
traces are intentionally not included.

## Stage 5 — OAuth hardening

Rotating refresh tokens made a naive refresh loop unsafe. The implementation
now uses one canonical token owner, a lock, atomic replacement, one-use callback
state, constant-time comparisons, quarantine for ambiguous refresh outcomes,
credential-free lifecycle metadata, and a direct handoff path for installation.

The public code documents the control model without publishing any token,
grant, host, timestamp, or production observation.

## Stage 6 — optional analysis

Gemini-backed analysis was added as a bounded, optional layer over deterministic
Python validation. The model may choose only declared functions; it cannot run
SQL, shell commands, arbitrary network requests, or mutations outside the
allowlisted tools. AI and image features are disabled by default in this export.

## Stage 7 — dashboard and infrastructure

The generated dashboard became a visual explanation of readiness, sleep,
activity, factors, and trends. Build contracts, escaped data binding, atomic
publication, and accessibility checks were added around it. The source includes
the template and UI modules, but never a generated personal dashboard or design
reference asset.

## How the work was developed

The project was built iteratively with a mixture of manual engineering and
agent-assisted work in Codex. That describes the development workflow, not an
exclusive authorship claim and not a promise that every historical experiment
is represented here. Raw prompts, internal traces, private task transcripts,
and agent workspace metadata are omitted.

## What this story is for

Readers can inspect the architecture, run synthetic tests, and understand why
the safety boundaries exist. They cannot reproduce the operator's private
health history, production host, credentials, or provider account state—and
that is intentional.
