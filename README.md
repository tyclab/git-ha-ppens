# git-ha-ppens (fork)

A fork of [manuveli/git-ha-ppens](https://github.com/manuveli/git-ha-ppens) for Home Assistant
configurations that are deployed from git. In that setup the git repository is the source of the
configuration: changes are reviewed and merged there, and Home Assistant pulls them and has to put
them into effect safely. Edits made in the Home Assistant UI are committed and pushed back.

This fork adds what that needs on top of upstream: a pull is applied (reload, restart or nothing),
nothing unreviewed is merged or force-pushed, secrets are never committed, and the Git services are
for administrators only.

Upstream's README for the base version describes the features, setup and options this fork keeps:
[README at v0.7.0](https://github.com/manuveli/git-ha-ppens/blob/v0.7.0/README.md).

## Base version

The branch `tyc` (the default branch) is upstream release **v0.7.0** plus the patches below, one
commit per patch. `main` and the older tags are upstream's history and are not released from here.
Upstream's later releases (1.x) add features of their own; this fork has not been moved onto them,
and installing one of them over this fork removes the patches.

## What the patches add

The patched code is marked `LOCAL PATCH` in the source.

1. **Fail closed when the pre-deploy check cannot run.** If the configuration check itself raises,
   the pull is blocked and rolled back instead of going ahead unchecked. The reason shows in the
   "Pull blocked" notification.
2. **Apply a pull from its diff.** Each changed file, and each changed top-level key of
   `configuration.yaml` and `packages/`, is mapped to a reload, a restart or nothing. A new or
   changed item with a start trigger, or a changed helper `initial`, restarts, since a reload never
   applies those; a path the mapping does not know restarts. A reload that fails, logs an error or
   leaves a changed helper unapplied falls back to a restart. Changed YAML dashboards are refreshed,
   so open browser tabs see the new version. The `git_ha_ppens_pull` event carries `changed_files`
   and the applied plan (`apply`), for the automatic and the manual pull alike.
3. **Abort and alert on conflict.** A conflicted pull is aborted and the working tree stays on its
   last commit; a push rejected because the remote moved stops. Upstream instead left conflict
   markers on disk, or merged unrelated histories and force-pushed. Either case raises a
   "Sync blocked" notification and an error event.
4. **Refuse to commit secret-shaped paths.** Before any commit the integration makes (auto-commit,
   the commit and sync services, the backup commit before a pull), staged paths such as
   `secrets.yaml`, `.storage/`, `.ssh/`, `*.bak*` or private keys are refused: the index is reset,
   nothing is committed or pushed, and a "Commit refused" notification names the paths.
5. **Push what a pull healed.** With auto-push on, commits that a rejected push left behind are
   pushed after the auto-pull that resolves the divergence, and after any fetch that finds the
   branch ahead and not behind.
6. **Admin-only services.** All Git services are registered as admin services: a call from a
   non-admin user is refused before Git runs. Administrators and automations keep access.
7. **Flash pulled ESPHome nodes.** A pull that changes a node configuration under `esphome/` compiles
   and flashes that node over the air through Home Assistant's ESPHome dashboard link, one node at a
   time; a change in a subdirectory of `esphome/` flashes every node. The first failure stops the
   rest and raises a notification. The admin service `git_ha_ppens.flash_esphome` retries.
8. **Stop the file watcher with Home Assistant.** The file watcher stops when Home Assistant stops,
   so a restart no longer logs "Event loop is closed" errors.

The mapping in patch 2 knows one repository layout: `packages/`, `blueprints/`, `custom_sentences/`,
`dashboards/` and `esphome/` at the top level, and tooling files Home Assistant never reads (such as
`Makefile`, `tests/` or CI configuration). In a repository laid out differently, more pulls end in a
restart.

## Versions and tags

Releases are tagged `v<upstream base>.<n>`: the upstream version the fork is built on, plus a
fourth number counted up for each release of the fork. The first is `v0.7.0.1`. The `version` in
`custom_components/git_ha_ppens/manifest.json` matches the tag without the `v`.

A release is a GitHub release of a tag on `tyc`, with no attached file: an installer copies
`custom_components/git_ha_ppens` from the tagged source.

This repository has no tests and no CI of its own. The patches are tested in the configuration
repository that vendors this fork, before a release reaches a live system.

## Installation

In the Home Assistant Marketplace (HACS), add `https://github.com/tyclab/git-ha-ppens` as a custom
repository of type **Integration**, install **git-ha-ppens** from it and restart Home Assistant.

Do not track upstream's repository for the same integration at the same time: installing an upstream
update replaces these files and drops every patch. The fork keeps upstream's domain
(`git_ha_ppens`), so an existing config entry stays as it is.

For a manual install, copy `custom_components/git_ha_ppens` from a release tag into the
`custom_components` directory of your configuration and restart Home Assistant.

## License

MIT, as upstream. `LICENSE` is upstream's file, unchanged.
