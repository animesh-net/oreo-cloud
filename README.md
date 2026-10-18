# oreo-cloud

Everything that runs on the oreo-cloud Raspberry Pi 5 home server, kept under version control.

- Private repository.
- All logic ships as a single Docker image, built by GitHub Actions and pulled on the Pi. Nothing of ours runs outside Docker.
- One `oreo` command with a subcommand for every operation: backup, health checks, notifications, system checks and heartbeat.
- No secrets in this repository. Credentials stay on the Pi.

The external 5-minute monitor lives in the public repository `oreo-monitor`.

## Changes

`main` is protected. Every change goes through a pull request and is squash-merged by the owner.
