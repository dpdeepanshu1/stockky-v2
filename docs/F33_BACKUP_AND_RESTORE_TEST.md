# F33 (part 1) - nightly backup + restore test (2026-10-23)

`scripts/backup_state.sh` (run on the VM from `~/stockky-v2`):

| Command | What it does |
| --- | --- |
| `run` | tar.gz of the volumes `control-state-rt`, `control-state-ps` (Calibration switches), `runner-data`, `bar-records`; `pg_dump` of `DATABASE_URL` from `.env` (local `pg_dump`, else the `postgres:16-alpine` image); writes `MANIFEST.sha256`; verifies what it wrote; deletes backups older than `KEEP_DAYS` (14); optional `OFFSITE_CMD` (e.g. `rclone copy {dir} remote:stockky`) |
| `verify [DIR]` | checksums + every archive lists + `pg_restore --list` (newest backup by default) |
| `restore-test` | restores the newest backup of every volume into a scratch volume `<name>-restoretest`, compares the file count with the archive, deletes the scratch |
| `restore VOLUME FILE` | restores one archive into a NEW volume `<project>_VOLUME-restored`; never overwrites a live volume |
| `schedule` / `schedule-remove` | cron (IST): backup 02:30 daily, restore-test Sunday 03:00; touches only lines tagged `# stockky-backup` |

Fails loudly: nothing to back up, an empty manifest, a failed dump (the DB password is masked in the message), a failed offsite copy all exit non-zero. Without `OFFSITE_CMD` it says the backup is only on the same VM.
Not saved on purpose: scrip-master caches and Hugging Face model caches (they rebuild). The hosted Postgres' own automatic backups stay ON (vm_ops checklist item 3); this dump is the extra copy you control.
Tests: `scripts/test_backup_state.py` (9, pass in the sandbox with a fake docker; added to the CI script list). Not run: real docker volumes, a real `pg_dump`, cron.
First use on the VM: `bash scripts/backup_state.sh run`, then `restore-test`, then `schedule`. Check the project prefix with `docker volume ls | grep control-state` (set `COMPOSE_PROJECT_NAME` if it differs from the folder name).
