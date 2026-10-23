# Deployment conventions

- Prefer host bind mounts for persistent service state instead of Docker named volumes, so data is visible and can be backed up directly.
- Group files by service. Keep Tailscale configuration in `tailscale/config/` and private runtime state in `tailscale/state/`. Keep the development home in `dev-runtime/home/` and SSH host keys in `dev-runtime/ssh-host-keys/`.
- Track configuration and scripts; exclude credentials, private keys, runtime state, and user home data from Git and Docker build contexts.
- Preserve existing SSH host keys and Tailscale state when changing images, recreating containers, or moving storage. Stop the affected service before moving live state, and verify its identity after restarting.
- Use `scripts/dev.py` to start the deployment with its generated dependency mounts. The existing dependency/cache volume is an intentional exception to the bind-mount preference; do not migrate it without a separate request.
