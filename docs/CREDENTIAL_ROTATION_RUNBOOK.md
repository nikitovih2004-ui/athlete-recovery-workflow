# Credential rotation runbook

This is a planning document. It does not execute a rotation and never asks a
reader to print, paste, or commit a credential. Treat every secret that existed
in the old private history as compromised before any public launch.

## Preparation

1. Freeze publication and deployments.
2. Inventory live consumers, operators, secret stores, logs, backups, CI, and
   local machines without copying values into tickets or chat.
3. Take an encrypted, access-restricted configuration backup and record only
   owner, system, and rotation date.
4. Prepare a rollback window and a synthetic smoke test for each integration.

## WHOOP

1. Revoke every old user grant and access/refresh pair.
2. Rotate or recreate the application secret as the provider permits.
3. Update the private runtime configuration through the approved secret store.
4. Complete a fresh offline authorization and secure token handoff.
5. Verify the old pair fails, the new pair syncs synthetic/approved data, and
   the refresh quarantine is empty.

## Telegram

1. Revoke and regenerate the bot token through the official bot-management
   interface.
2. Update the private runtime configuration and restart the bot service.
3. Run an authorized private-chat smoke test; do not use a group or public
   channel.
4. Verify the old token fails and logs contain no URL or credential material.

## Gemini and relay

1. Create a least-privilege replacement direct key if direct mode is needed.
2. If relay mode is used, create a replacement bearer secret and bind it to the
   reviewed secret manager/runtime identity.
3. Update both relay and client configuration, then run a synthetic readiness
   probe with no health or personal text.
4. Revoke old keys/bearers, delete unused long-lived cloud keys, and review
   service-account permissions.
5. Confirm logs and provider diagnostics contain only safe categories and opaque
   request IDs.

## SSH and host access

1. Add a replacement deployment key and verify the pinned host key.
2. Update the private deployment configuration and perform a non-destructive
   connection check.
3. Remove the old authorized key, agent copy, password, and local backup.
4. Rotate the host key only if its private material was exposed, coordinating
   the known-hosts update with the operator.

## Close-out

- Scan live logs, backups, CI artifacts, and secret stores for old values without
  printing them.
- Confirm old credentials fail and new credentials work.
- Record date, owner, systems, and evidence locations—not secret values.
- Re-run the fresh-clone public scan before creating the new repository.

Do not make the repository public until all required rotations and provider/legal
reviews are complete.
