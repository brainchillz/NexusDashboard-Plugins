# NexusDashboard-Plugins

Out-of-tree **drop-in plugins** for the [Nexus Dashboard][dashboard]. Each
top-level directory is one plugin: copy it into a node's plugin directory,
restart the service, enable it on the Modules page. Nothing here is part of
the dashboard's core — no plugin in this repo requires a change to any file
in the dashboard tree.

Requires dashboard **3.0.0 or newer** (the plugin system). See `PLUGINS.md`
in the dashboard repo for the full plugin contract.

[dashboard]: https://github.com/brainchillz/NexusDashboard-Modular

## Why these live outside the dashboard repo

The dashboard is deployed across a fleet of mixed hardware, and its own
`plugins/` directory is untracked by design (installed by root over SSH, never
through the web UI). Anything vendor- or node-specific therefore has no home in
that tree: shipping it as a built-in module would push dead weight to every
node that will never have the hardware. This repo is that home.

## Plugins

| Plugin | Tier | What it does | Needs |
|---|---|---|---|
| [`drive-bays`](drive-bays/) | Python | Physical drive-bay map for SES-capable enclosures — every drive shown in the bay it occupies, with identity, ZFS/LVM usage, SMART, and real identify/fault LED control. Also groups NVMe on bifurcated M.2 carrier cards by PCIe slot | A backplane or HBA exposing SCSI Enclosure Services |

## Installing a plugin

`<app dir>` is `/opt/nexus-dashboard` on a fresh install; an in-place-upgraded
node keeps its original `/opt/storage-dashboard` naming. The unit name matches
the directory.

```sh
sudo cp -r <plugin> <app dir>/plugins/
sudo chown -R root:root <app dir>/plugins/<plugin>
sudo chmod -R go-w      <app dir>/plugins/<plugin>
sudo systemctl restart <unit>
```

The loader refuses world-writable plugin files, hence the `chmod`. Plugins
always start **disabled** — enable it on the Modules page (or
`POST /api/modules {"id": "<plugin>", "enabled": true}`).

A plugin that fails to load never prevents boot: it appears on the Modules
page as *load failed*, with the reason in `GET /api/plugins`.

Uninstall = remove the directory and restart.

### Sudoers

The dashboard **never** installs plugin sudoers. Where a plugin needs one, its
own README gives the exact lines. Two rules apply everywhere:

- Use the two-line pattern (bare path, then path + a lone trailing `*`). A
  wildcard embedded inside an argument word is rejected by **sudo-rs**, the
  default sudo on Ubuntu 26.04+.
- **Always `visudo -cf` the file before installing it.** Both sudo flavours
  error-recover past a bad `sudoers.d` file, so breakage is silent.

## Conventions for plugins in this repo

- **Portable**: Python 3.9 (RHEL 9 / Rocky 9) through 3.14 (Ubuntu 26.04). No
  `match` statements, no PEP 604 unions, no walrus in comprehensions.
- **Degrade, never crash.** Missing hardware, a missing tool or a missing
  sudoers grant produces a clear message on the page, not a traceback.
- **Standard library plus the blessed SDK surface** (`nexusdash.core.runcmd`,
  `.config`, `.validators`, `.services`, `.registry`, `.auth._is_admin`).
  Everything else in `nexusdash.*` is internal and may change in any release.
- Argument-list `run()` — never a shell. Allowlist-validate every route
  argument. Any root helper re-validates its own arguments; it does not trust
  the caller.
- Frontend: no build step, no vendored framework. Reuse the dashboard's own
  classes and CSS variables so pages follow the theme in light and dark.

## License

Same terms as the Nexus Dashboard. See the dashboard repository.
